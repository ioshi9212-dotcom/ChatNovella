"""ChatNovella V2: small, durable, file-based interactive novel store.

The writer decides prose and meaning; the server only validates provenance,
ownership, file boundaries and transaction integrity. No legacy Lite imports.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from difflib import SequenceMatcher
from hashlib import sha256
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
from typing import Any

from . import relations as relationship_store
from . import threads as open_threads
from . import hooks as story_hooks
from . import scene_preflight

CHUNK_SIZE = 36_000  # Unicode code points, not UTF-8 bytes
SESSION = re.compile(r"^NV2_[0-9a-f]{24}$")
IDENT = re.compile(r"^[a-z][a-z0-9_]{1,49}$")
STATE_KEYS = {"turn", "datetime", "location", "pov", "present", "remote", "objects", "unfinished"}
ACTOR_KEYS = {"position", "clothing", "footwear", "items", "physical", "unfinished"}
CHARACTER_RULE = (
    "## Правило персонажа\n"
    "Персонаж действует, говорит и принимает решения в соответствии со своим "
    "исходным характером, прошлым, целями и обстоятельствами. Не смягчать, "
    "не исправлять и не подменять личность ради удобной или морально правильной "
    "реакции. Персонаж обладает собственной волей; его слова и действия "
    "могут расходиться с ожиданиями POV.\n\n"
    "## Авторский оригинал (дословно)\n"
)
NOVEL_RULE = (
    "## Правило этой новеллы\n"
    "Все значимые персонажи исходной анкеты должны получать естественное "
    "сюжетное развитие; не вводить ротации, обязательные появления и "
    "искусственные расписания. Авторский канон не исправлять и не смягчать.\n"
)
LEAK_RE = re.compile(
    r"(?iu)\b(?:узнал(?:а|и)?|выяснил(?:а|и)?|знает|помнит|"
    r"вспомнил(?:а|и)?|услышал(?:а|и)?\s+о\s+том|со\s+слов)\b"
)


class EngineError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


def need(ok: bool, code: str, message: str, status: int = 422) -> None:
    if not ok:
        raise EngineError(code, message, status)


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def knowledge_marker(fact: str, source: str) -> str:
    """Keep legacy one-line records; safely encode multiline literal evidence.

    The full unmodified source also remains in the archived turn.
    """
    shown = source.strip()
    if "\n" in source or "\r" in source:
        shown = "JSON " + json.dumps(source, ensure_ascii=False)
        # JSON escapes CR/LF and ASCII controls. Escape remaining Unicode
        # line separators so a quotation cannot create a fake memory row.
        for sep in ("\x85", "\u2028", "\u2029"):
            shown = shown.replace(sep, "\\u%04x" % ord(sep))
    return "- " + fact.strip() + " [источник: " + shown + "]"


def digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def slug(value: str) -> str:
    need(isinstance(value, str) and bool(IDENT.fullmatch(value)) and value != "pov",
         "INVALID_NAME", "Use a stable lowercase English character name, e.g. adrian")
    return value


def atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def remove_character_file(path: Path, name: str) -> None:
    need(bool(re.fullmatch(r"(?:characters|knowledge)/[a-z][a-z0-9_]{1,49}\.md", name))
         and not name.endswith("/pov.md"), "DELETE_PATH", "Only new NPC files can be removed")
    target = path / name
    target.unlink(missing_ok=True)
    if target.parent.exists():
        directory = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def safe_rel(name: str) -> bool:
    return (name in {"state.md", "relationships.md", "timeline.md", "history.jsonl",
                     "system/audit.jsonl", "novel.md", "open_threads.md", "hooks.md"}
            or bool(re.fullmatch(r"(?:characters|knowledge)/[a-z][a-z0-9_]{1,49}\.md", name)))


def actor(value: Any) -> dict:
    need(isinstance(value, dict) and set(value) == ACTOR_KEYS,
         "ACTOR_FIELDS", "Physical actor state has fixed fields")
    for key in ("position", "clothing", "footwear", "physical"):
        need(isinstance(value[key], str) and len(value[key]) <= 240,
             "ACTOR_TEXT", "Actor fields must be short text")
        need(not LEAK_RE.search(value[key]), "STATE_KNOWLEDGE_LEAK",
             "Personal knowledge cannot be written into state")
    for key in ("items", "unfinished"):
        short_list(value[key], key)
    return deepcopy(value)


def short_list(value: Any, label: str) -> None:
    need(isinstance(value, list) and len(value) <= 60 and
         all(isinstance(item, str) and 0 < len(item) <= 240 and
             not LEAK_RE.search(item) for item in value),
         "STATE_LIST", label + " must contain only short physical descriptions")


def validate_state(value: Any, characters: set[str], expected_turn: int) -> dict:
    need(isinstance(value, dict) and set(value) == STATE_KEYS,
         "STATE_FIELDS", "State has only 8 strict scene fields")
    need(type(value["turn"]) is int and value["turn"] == expected_turn,
         "STATE_TURN", "State turn does not match the committed turn")
    need(value["datetime"] is None or
         (isinstance(value["datetime"], str) and len(value["datetime"]) <= 60),
         "STATE_DATE", "Invalid game date/time")
    need(isinstance(value["location"], str) and 0 < len(value["location"]) <= 240 and
         not LEAK_RE.search(value["location"]), "STATE_LOCATION", "Invalid scene location")
    if value["datetime"] is not None:
        try:
            datetime.fromisoformat(value["datetime"])
        except ValueError:
            raise EngineError("STATE_DATE", "Game datetime must be ISO 8601", 422)
    actor(value["pov"])
    present, remote = value["present"], value["remote"]
    need(isinstance(present, dict) and isinstance(remote, dict),
         "STATE_CAST", "Present and remote must be mappings")
    need(not (set(present) & set(remote)) and
         set(present).issubset(characters) and set(remote).issubset(characters) and
         "pov" not in present and "pov" not in remote,
         "STATE_CAST", "Unknown or duplicated scene participant")
    for physical in present.values():
        actor(physical)
    for description in remote.values():
        need(isinstance(description, str) and 0 < len(description) <= 120 and
             not LEAK_RE.search(description), "STATE_REMOTE",
             "Remote specifies a communication channel, not knowledge")
    short_list(value["objects"], "objects")
    short_list(value["unfinished"], "unfinished")
    return deepcopy(value)


def validate_relations(value: Any, characters: set[str]) -> dict:
    try:
        return relationship_store.normalize(value, characters)
    except relationship_store.RelationProblem as exc:
        raise EngineError("RELATION_INVALID", str(exc), 422) from exc


def empty_actor() -> dict:
    return {"position": "", "clothing": "", "footwear": "", "items": [],
            "physical": "", "unfinished": []}


def empty_state() -> dict:
    return {"turn": 0, "datetime": None, "location": "НЕ ЗАДАНА",
            "pov": empty_actor(), "present": {}, "remote": {},
            "objects": [], "unfinished": []}


def chunks(value: str) -> list[str]:
    return [value[i:i + CHUNK_SIZE] for i in range(0, len(value), CHUNK_SIZE)] or [""]


def _backward_history(path: Path):
    """Yield archive JSONL rows newest first without loading the whole archive."""
    with path.open("rb") as handle:
        cursor = handle.seek(0, os.SEEK_END)
        remainder = b""
        while cursor:
            size = min(16_384, cursor)
            cursor -= size
            handle.seek(cursor)
            pieces = (handle.read(size) + remainder).split(b"\n")
            remainder = pieces[0]
            for line in reversed(pieces[1:]):
                if line.strip():
                    yield json.loads(line.decode("utf-8"))
        if remainder.strip():
            yield json.loads(remainder.decode("utf-8"))


def _recent_turns(path: Path, count: int) -> list[dict]:
    """Read only enough archive tail rows for the latest active turns."""
    result: list[dict] = []
    reverted: set[str] = set()
    for row in _backward_history(path / "history.jsonl"):
        if row["kind"] == "rollback":
            reverted.add(row["reverts"])
        elif row["kind"] == "turn":
            if row["request_id"] in reverted:
                reverted.remove(row["request_id"])
            else:
                result.append(row)
                if len(result) == count:
                    break
    return list(reversed(result))


def _archived_turn(path: Path, request_id: str) -> dict | None:
    """A receipt points into the immutable archive; search from its newest end."""
    reverted: set[str] = set()
    for row in _backward_history(path / "history.jsonl"):
        if row["kind"] == "rollback":
            reverted.add(row["reverts"])
        elif row["kind"] == "turn":
            if row["request_id"] == request_id:
                return None if request_id in reverted else row
    return None


# The recent response cache stays small. A fixed-size Bloom filter prevents
# reading the full scene archive for the common case of a fresh request ID.
# For a possible old repeat, search the authoritative archive and compare its
# exact saved request hash. False positives cost a lookup, never data loss.
RECENT_REQUESTS = 32
REQUEST_FILTER_BYTES = 8192


def _request_bits(request_id: str) -> list[int]:
    raw = sha256(request_id.encode("utf-8")).digest()
    width = REQUEST_FILTER_BYTES * 8
    return [int.from_bytes(raw[n:n + 4], "big") % width for n in (0, 4, 8)]


def _seen_request(meta: dict, request_id: str) -> bool:
    if request_id in meta.get("requests", {}):
        return True
    raw = meta.get("request_filter")
    if not raw:
        # Legacy sessions retained every request in the old metadata map.
        return False
    bitmap = base64.b64decode(raw)
    return all(bitmap[pos // 8] & (1 << (pos % 8))
               for pos in _request_bits(request_id))


def _remember_request(new: dict, request_id: str) -> None:
    raw = new.get("request_filter")
    if raw:
        bitmap = bytearray(base64.b64decode(raw))
    else:
        bitmap = bytearray(REQUEST_FILTER_BYTES)
        for prior in new.get("requests", {}):
            for pos in _request_bits(prior):
                bitmap[pos // 8] |= 1 << (pos % 8)
    for pos in _request_bits(request_id):
        bitmap[pos // 8] |= 1 << (pos % 8)
    new["request_filter"] = base64.b64encode(bitmap).decode("ascii")
    cache = new["requests"]
    if len(cache) > RECENT_REQUESTS:
        newest = sorted(cache, key=lambda k: cache[k]["result"]["turn"],
                        reverse=True)[:RECENT_REQUESTS]
        new["requests"] = {key: cache[key] for key in newest}


def _archived_request(path: Path, request_id: str) -> tuple[dict | None, bool]:
    reverted = False
    for row in _backward_history(path / "history.jsonl"):
        if row["kind"] == "rollback" and row["reverts"] == request_id:
            reverted = True
        if row["kind"] == "turn" and row["request_id"] == request_id:
            return row, reverted
    return None, False


def _append_descriptor(path: Path, text: str) -> dict:
    """Commit an append by size+tail fingerprint, not by copying all old turns."""
    data = text.encode("utf-8")
    with path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        tail_size = min(2048, size)
        handle.seek(size - tail_size)
        tail_sha = sha256(handle.read(tail_size)).hexdigest()
    return {"size": size, "tail_sha": tail_sha,
            "text": base64.b64encode(data).decode("ascii")}


def _apply_append(path: Path, entry: dict) -> None:
    """Crash-safe, replayable append. A partially written tail is repaired once."""
    expected = entry["size"]
    raw = base64.b64decode(entry["text"])
    with path.open("r+b") as handle:
        size = handle.seek(0, os.SEEK_END)
        need(expected <= size <= expected + len(raw), "ARCHIVE_DIVERGED",
             "Archive size changed outside this transaction")
        tail_size = min(2048, expected)
        handle.seek(expected - tail_size)
        need(sha256(handle.read(tail_size)).hexdigest() == entry["tail_sha"],
             "ARCHIVE_CORRUPTED", "Archive prefix changed; refusing append")
        if size == expected + len(raw):
            handle.seek(expected)
            need(handle.read(len(raw)) == raw, "ARCHIVE_DIVERGED",
                 "Archive tail does not match the pending transaction")
            return
        handle.truncate(expected)
        handle.seek(expected)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _near_duplicate(scene: str, older: list[dict]) -> bool:
    """Cheap warning only. Short recurring dialogue is never a hard error."""
    if len(scene) < 180 or len(scene.split()) < 20:
        return False
    current = scene.split()
    for row in older[-3:]:
        previous = row.get("narration", "")
        if len(previous) < 180 or abs(len(previous) - len(scene)) > len(scene) // 10:
            continue
        if previous == scene:
            return True
        if SequenceMatcher(None, current[:500], previous.split()[:500],
                           autojunk=True).ratio() >= 0.97:
            return True
    return False


def _speech_knowledge_flags(novel: str, scene: str, relationships: dict) -> list[str]:
    """Conservative review cues only, not an automated semantic truth detector.

    A character may learn an unfamiliar name during this very scene, ask
    about an unknown person, lie, or be mistaken. Never reject prose by regex.
    """
    label_map: dict[str, str] = {"pov": "pov", "POV": "pov"}
    for slug_name, display in re.findall(
            r"(?m)^- ([a-z][a-z0-9_]{1,49}) — ([^\n]+)$", novel):
        label_map[display.strip().casefold()] = slug_name
        label_map[slug_name.casefold()] = slug_name
    active = relationships.get("familiarity", {})
    display_names = [(slug_name, display.strip()) for slug_name, display in
                     re.findall(r"(?m)^- ([a-z][a-z0-9_]{1,49}) — ([^\n]+)$", novel)]
    flagged: set[str] = set()
    for line in scene.splitlines():
        utterance = re.match(r"^\s*\*\*([^*\n]+)\*\*\s*[—–-]\s*(.+)$", line)
        if not utterance:
            continue
        source = label_map.get(utterance.group(1).strip().casefold())
        if source is None:
            continue
        for target, display in display_names:
            if source == target or active.get(source, {}).get(target, "unaware") != "unaware":
                continue
            if re.search(r"(?<!\w)" + re.escape(display) + r"(?!\w)",
                         utterance.group(2), re.IGNORECASE):
                flagged.add("POSSIBLE_UNFAMILIAR_NAME:" + source + ":" + target)
    return sorted(flagged)


class Engine:
    def __init__(self, root: str | Path, instructions: str | Path | None = None,
                 *, scene_review_enabled: bool | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.instructions = Path(instructions or Path(__file__).parent)
        if scene_review_enabled is None:
            value = os.getenv("CHATNOVELLA_V2_SCENE_REVIEW", "0").strip().lower()
            if value not in {"0", "1", "false", "true", "off", "on"}:
                raise ValueError("CHATNOVELLA_V2_SCENE_REVIEW must be 0 or 1")
            scene_review_enabled = value in {"1", "true", "on"}
        self.scene_review_enabled = scene_review_enabled

    def _dir(self, sid: str) -> Path:
        need(bool(SESSION.fullmatch(sid)), "INVALID_SESSION", "Invalid session id")
        path = self.root / sid
        need(path.is_dir(), "SESSION_NOT_FOUND", "Unknown session", 404)
        return path

    @staticmethod
    def _read(path: Path, name: str) -> str:
        # read_text() normalizes CRLF: original author bytes must remain literal.
        return (path / name).read_bytes().decode("utf-8")

    def load_instructions(self, rules_only: bool = False) -> dict:
        """Full instruction texts for explicit inspection, or rules-only bootstrap.

        This does not persist ChatGPT's chat memory. The client retains the
        returned text in its current conversation context.
        """
        rules = self._read(self.instructions, "rules.md")
        if rules_only:
            return {"rules": {"content": rules, "sha256": digest(rules), "chars": len(rules)},
                    "refresh_policy": "rules_first_skin_last_per_turn",
                    "scene_review_enabled": self.scene_review_enabled}
        skin = self._read(self.instructions, "skin_builder.md")
        return {
            "rules": {"content": rules, "sha256": digest(rules),
                      "chars": len(rules)},
            "skin_builder": {"content": skin, "sha256": digest(skin),
                             "chars": len(skin), "empty": not bool(skin.strip())},
            "refresh_policy": "rules_first_skin_last_per_turn",
            "scene_review_enabled": self.scene_review_enabled,
        }

    def instruction_versions(self) -> dict:
        """Small explicit version check; no instruction texts in the response."""
        data = self.load_instructions()
        return {
            "rules_sha256": data["rules"]["sha256"],
            "skin_builder_sha256": data["skin_builder"]["sha256"],
            "skin_builder_empty": data["skin_builder"]["empty"],
            "scene_review_enabled": self.scene_review_enabled,
        }

    @staticmethod
    def _cast(path: Path) -> set[str]:
        return {p.stem for p in (path / "characters").glob("*.md")
                if p.stem != "pov" and IDENT.fullmatch(p.stem)}

    def _recover(self, path: Path, meta: dict) -> dict:
        pending = meta.get("pending")
        if pending:
            for name, encoded in pending["files"].items():
                need(safe_rel(name), "RECOVERY_PATH", "Invalid pending file")
                atomic(path / name, base64.b64decode(encoded).decode("utf-8"))
            for name in pending.get("deletes", []):
                remove_character_file(path, name)
            for name, entry in pending.get("appends", {}).items():
                need(name == "history.jsonl", "RECOVERY_PATH",
                     "Only the scene archive supports pending appends")
                _apply_append(path / name, entry)
            meta = pending["next"]
            atomic(path / "system/meta.json", dump(meta))
        return meta

    @contextmanager
    def _locked(self, sid: str):
        path = self._dir(sid)
        lockpath = path / "system/lock"
        lockpath.parent.mkdir(parents=True, exist_ok=True)
        with lockpath.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                meta = json.loads(self._read(path, "system/meta.json"))
                yield path, self._recover(path, meta)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _version(meta: dict, expected: int) -> None:
        need(type(expected) is int and expected == meta["version"], "STALE_VERSION",
             "Session version changed; prepare a fresh context", 409)

    @staticmethod
    def _write(path: Path, meta: dict, files: dict[str, str], new: dict,
               appends: dict[str, str] | None = None,
               deletes: list[str] | None = None) -> None:
        appends = appends or {}
        deletes = deletes or []
        need(len(deletes) == len(set(deletes)) and all(
            bool(re.fullmatch(r"(?:characters|knowledge)/[a-z][a-z0-9_]{1,49}\.md", n))
            and not n.endswith("/pov.md") and n not in files and n not in appends
            for n in deletes), "DELETE_PATH", "Invalid new NPC rollback files")
        for name in files:
            need(safe_rel(name) and name not in appends, "PATH_NOT_ALLOWED",
                 "Write outside session documents or conflicting write modes")
        need(set(appends).issubset({"history.jsonl"}), "PATH_NOT_ALLOWED",
             "Append allowed only for the full scene archive")
        append_specs = {k: _append_descriptor(path / k, value)
                        for k, value in appends.items()}
        pending = {"files": {k: base64.b64encode(v.encode("utf-8")).decode("ascii")
                             for k, v in files.items()},
                   "appends": append_specs, "deletes": deletes, "next": new}
        atomic(path / "system/meta.json", dump({**meta, "pending": pending}))
        for name, content in files.items():
            atomic(path / name, content)
        for name in deletes:
            remove_character_file(path, name)
        for name, descriptor in append_specs.items():
            _apply_append(path / name, descriptor)
        atomic(path / "system/meta.json", dump(new))

    def create(self, original: str, blocks: list[dict], names: dict[str, str],
               genre_answers: dict[str, str] | None = None,
               relationships: dict | None = None,
               initial_threads: list[dict] | None = None,
               initial_hooks: list[dict] | None = None) -> dict:
        need(isinstance(original, str) and bool(original.strip()), "ORIGINAL_REQUIRED",
             "Complete original author's text is required")
        need(isinstance(names, dict) and all(isinstance(v, str) and v.strip()
             for v in names.values()), "CAST_NAMES", "Character display names required")
        for name in names:
            if name != "pov":
                slug(name)
        need(isinstance(blocks, list) and bool(blocks), "SOURCE_BLOCKS",
             "Exact source positions or unique text anchors are required")
        if all(isinstance(row, dict) and set(row) == {"anchor", "target"}
               for row in blocks):
            offsets = []
            for row in blocks:
                anchor = row["anchor"]
                need(isinstance(anchor, str) and bool(anchor) and original.count(anchor) == 1,
                     "SOURCE_ANCHOR", "Each anchor must occur exactly once in the original")
                offsets.append(original.index(anchor))
            need(offsets[0] == 0 and offsets == sorted(set(offsets)),
                 "SOURCE_ANCHOR", "Anchors must start at character zero and follow source order")
            blocks = [{"start":offsets[i],
                       "end":offsets[i + 1] if i + 1 < len(offsets) else len(original),
                       "target":row["target"]} for i, row in enumerate(blocks)]
        partitions: dict[str, list[str]] = {"novel": [], "pov": []}
        for name in names:
            partitions[name] = []
        position = 0
        for block in blocks:
            need(isinstance(block, dict) and set(block) == {"start", "end", "target"},
                 "SOURCE_BLOCK", "Each block needs start, end and target only")
            start, end, target = block["start"], block["end"], block["target"]
            need(type(start) is int and type(end) is int and start == position and
                 start < end <= len(original) and target in partitions, "SOURCE_COVERAGE",
                 "Blocks must exactly partition original in order without gaps/overlaps")
            partitions[target].append(original[start:end])
            position = end
        need(position == len(original), "SOURCE_COVERAGE",
             "Every single character of the original must be assigned")
        need(isinstance(genre_answers or {}, dict) and all(isinstance(k, str) and
             isinstance(v, str) for k, v in (genre_answers or {}).items()),
             "GENRE_ANSWERS", "Genre settings must be plain text")
        cast_text = "\n## Персонажи (постоянные имена файлов)\n" + "".join(
            "- " + name + " — " + display.strip() + "\n"
            for name, display in names.items())
        extra = ("\n## Утверждаемые настройки жанров\n" +
                 "".join("- " + k + ": " + v + "\n"
                         for k, v in (genre_answers or {}).items())) if genre_answers else ""
        novel = NOVEL_RULE + "\n## Авторская анкета (дословные фрагменты)\n" + \
                "".join(partitions["novel"]) + "\n" + cast_text + extra
        candidate_relations = validate_relations(
            relationships or {"npc_to_pov": {}, "npc_to_npc": {}}, set(names) - {"pov"})
        # Plot memory is derived from canon, not a prerequisite for saving it.
        # Keep valid legacy seeds for older clients; defer invalid ones explicitly.
        story_memory_warnings = []
        initial_open_threads = open_threads.HEADER
        initial_hook_ledger = story_hooks.HEADER
        try:
            initial_open_threads = open_threads.seed(initial_threads, original)
            open_threads.parse(initial_open_threads)
        except (open_threads.ThreadProblem, TypeError, KeyError) as exc:
            initial_open_threads = open_threads.HEADER
            story_memory_warnings.append({"field": "initial_threads",
                                          "detail": str(exc)})
        try:
            candidate_hooks = story_hooks.seed(initial_hooks, original,
                                               set(names) - {"pov"})
            story_hooks.validate_links(story_hooks.parse(candidate_hooks),
                                       open_threads.parse(initial_open_threads))
            initial_hook_ledger = candidate_hooks
        except (story_hooks.HookProblem, TypeError, KeyError) as exc:
            story_memory_warnings.append({"field": "initial_hooks",
                                          "detail": str(exc)})
        sid = "NV2_" + secrets.token_hex(12)
        tmp = Path(tempfile.mkdtemp(prefix=".new-", dir=self.root))
        try:
            files = {"original.txt": original, "novel.md": novel,
                     "relationships.md": dump(candidate_relations),
                     "state.md": dump(empty_state()),
                     "timeline.md": "# Хронология\n",
                     "open_threads.md": initial_open_threads,
                     "hooks.md": initial_hook_ledger,
                     "history.jsonl": "", "system/audit.jsonl": ""}
            for name in set(names) | ({"pov"} if partitions["pov"] else set()):
                files["characters/" + name + ".md"] = CHARACTER_RULE + "".join(partitions[name])
                files["knowledge/" + name + ".md"] = "# Новые знания: " + name + "\n"
            files["knowledge/pov.md"] = "# Новые знания: pov\n"
            for name, content in files.items():
                atomic(tmp / name, content)
            canon_hash = digest(novel + "\n" + "".join(
                files["characters/" + name + ".md"] for name in sorted(names)))
            meta = {"session_id": sid, "version": 1, "turn": 0, "approved": False,
                    "canon_hash": canon_hash, "original_hash": digest(original),
                    "novel_hash": digest(novel),
                    "relation_rule_hash": digest(candidate_relations["rules"]),
                    "relationship_file_hash": digest(files["relationships.md"]),
                    "hook_file_hash": digest(initial_hook_ledger),
                    "first_seen": {},
                    "card_hashes": {name: digest(files["characters/" + name + ".md"])
                                    for name in set(names) | ({"pov"} if partitions["pov"] else set())},
                    "segments": deepcopy(blocks), "packet": None, "audit_due": 0,
                    "requests": {}, "last_undo": None}
            atomic(tmp / "system/meta.json", dump(meta))
            os.rename(tmp, self.root / sid)
            result = self.status(sid)
            if story_memory_warnings:
                result["story_memory_warnings"] = story_memory_warnings
                result["story_memory_instruction"] = (
                    "Canon saved. After approval, recover deferred story memory "
                    "from original using backfill_v2_hooks; do not recreate the novel.")
            return result
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def status(self, sid: str) -> dict:
        with self._locked(sid) as (_, meta):
            return {k: meta[k] for k in
                    ("session_id", "version", "turn", "approved", "canon_hash", "audit_due")}

    def approve(self, sid: str, expected_version: int, canon_hash: str,
                confirmation: str) -> dict:
        with self._locked(sid) as (path, meta):
            self._version(meta, expected_version)
            need(not meta["approved"] and isinstance(confirmation, str) and
                 bool(confirmation.strip()) and canon_hash == meta["canon_hash"],
                 "APPROVAL_REQUIRED", "Approve exact canon checksum explicitly")
            need(digest(self._read(path, "original.txt")) == meta["original_hash"] and
                 digest(self._read(path, "novel.md")) == meta["novel_hash"],
                 "CANON_CHANGED", "Original or main questionnaire changed before approval")
            for name, checksum in meta["card_hashes"].items():
                need(digest(self._read(path, "characters/" + name + ".md")) == checksum,
                     "CANON_CHANGED", "A character card changed before approval")
            new = {**meta, "approved": True, "version": meta["version"] + 1,
                   "approval": confirmation}
            self._write(path, meta, {}, new)
            return {"approved": True, "version": new["version"]}

    def set_opening(self, sid: str, expected_version: int, state: dict,
                    relationships: dict | None = None) -> dict:
        with self._locked(sid) as (path, meta):
            self._version(meta, expected_version)
            need(meta["turn"] == 0, "ALREADY_STARTED", "Opening is only editable before turn 1")
            cast = self._cast(path)
            state = validate_state(state, cast, 0)
            files = {"state.md": dump(state)}
            if meta.get("relationship_file_hash"):
                need(digest(self._read(path, "relationships.md")) ==
                     meta["relationship_file_hash"], "RELATION_FILE_CHANGED",
                     "Opening relationships were changed outside the engine")
            if relationships is not None:
                validated = validate_relations(relationships, cast)
                existing = validate_relations(
                    json.loads(self._read(path, "relationships.md")), cast)
                if meta.get("relation_rule_hash"):
                    need(digest(existing["rules"]) == meta["relation_rule_hash"],
                         "RELATION_RULE_CHANGED",
                         "Stored relationship rule was changed outside engine")
                need(validated["rules"] == existing["rules"], "RELATION_RULE_IMMUTABLE",
                     "Opening edit cannot replace established relationship rules")
                files["relationships.md"] = dump(validated)
            new = {**meta, "version": meta["version"] + 1, "packet": None}
            if "relationships.md" in files:
                new["relationship_file_hash"] = digest(files["relationships.md"])
            self._write(path, meta, files, new)
            return {"version": new["version"], "opening_saved": True}

    @staticmethod
    def _character_proposals(items: list[dict] | None, cast: set[str]) -> dict[str, dict]:
        items = [] if items is None else items
        need(isinstance(items, list) and len(items) <= 32, "CHARACTER_PROPOSALS",
             "Supply at most 32 new NPC proposals per turn")
        proposals = {}
        for row in items:
            need(isinstance(row, dict) and set(row) ==
                 {"name", "display", "established_text"}, "CHARACTER_PROPOSALS",
                 "Proposal needs name/display/established_text")
            name = slug(row["name"])
            display, source = row["display"], row["established_text"]
            need(name not in cast and name not in proposals, "CHARACTER_EXISTS",
                 "New character ID must be unique and not already registered")
            need(isinstance(display, str) and 0 < len(display.strip()) <= 120
                 and "\n" not in display and "\r" not in display
                 and isinstance(source, str) and 8 <= len(source.strip()) <= 4000,
                 "CHARACTER_REQUIRED", "New NPC needs a short name and literal final scene excerpt")
            proposals[name] = {"name": name, "display": display.strip(),
                               "established_text": source}
        return proposals

    @staticmethod
    def _with_new_cast(novel: str, proposals: dict[str, dict]) -> str:
        if not proposals:
            return novel
        heading = "## Персонажи (постоянные имена файлов)\n"
        pos = novel.find(heading)
        need(pos >= 0, "CAST_HEADING", "Main novel must contain the cast list")
        boundary = novel.find("\n## ", pos + len(heading))
        insert_at = len(novel) if boundary < 0 else boundary
        additions = "".join("- " + name + " — " + row["display"] + "\n"
                            for name, row in sorted(proposals.items()))
        return novel[:insert_at].rstrip("\n") + "\n" + additions + novel[insert_at:]

    def add_character(self, sid: str, expected_version: int, name: str,
                      display: str, established_text: str) -> dict:
        """Validate a proposal, never create persistent NPC files."""
        with self._locked(sid) as (path, meta):
            self._version(meta, expected_version)
            need(meta["approved"], "NOT_APPROVED", "Approve novel before play")
            proposal = self._character_proposals([{"name": name, "display": display,
                "established_text": established_text}], self._cast(path))[name]
            return {"name": name, "version": meta["version"], "added": False,
                    "proposal": proposal,
                    "instruction": "Pass proposal in prepare.new_characters. Only final successful commit registers NPC."}

    @staticmethod
    def _active(path: Path) -> list[dict]:
        active: list[dict] = []
        for line in (path / "history.jsonl").read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["kind"] == "turn":
                active.append(row)
            elif row["kind"] == "rollback":
                need(bool(active) and active[-1]["request_id"] == row["reverts"],
                     "HISTORY_INTEGRITY", "Invalid rollback marker")
                active.pop()
        return active

    @staticmethod
    def _source_signature(path: Path, relevant: set[str], kind: str) -> list[list]:
        names = ["novel.md", "state.md", "relationships.md", "timeline.md"]
        if (path / "open_threads.md").exists():
            names.append("open_threads.md")
        if (path / "hooks.md").exists():
            names.append("hooks.md")
        if kind == "canon":
            names.append("original.txt")
        for name in sorted(relevant):
            names.extend(("characters/" + name + ".md", "knowledge/" + name + ".md"))
        names.append("knowledge/pov.md")
        if (path / "characters/pov.md").exists():
            names.append("characters/pov.md")
        # Instruction snapshots have separate version hashes in the packet.
        sources = [path / name for name in names]
        return [[str(p), sha256(p.read_bytes()).hexdigest()] for p in sources]

    @staticmethod
    def _packet_stamp(path: Path) -> str:
        # Mounted storage can settle timestamps after fsync/rename. Compare
        # the actual bytes so metadata changes cannot invalidate a read packet.
        return sha256((path / "system/packet.json").read_bytes()).hexdigest()

    @staticmethod
    def _manifest(packet: dict, audit_due: int, reused: bool = False) -> dict:
        count = len(packet["hashes"])
        read = set(packet["received"])
        unread = [i for i in range(count) if i not in read]
        return {"packet_id": packet["id"], "version": packet["version"],
                "chunk_count": count, "chunk_size": CHUNK_SIZE,
                "chunk_hashes": packet["hashes"], "sha256": packet["sha256"],
                "participants": packet["participants"], "audit_due": audit_due,
                "request_id": packet.get("request_id"),
                 "instructions_mode": packet.get("instructions_mode"),
                 "skin_chunk_start": packet.get("skin_chunk_start"),
                 "rules_sha256": packet.get("rules_sha256"),
                 "skin_sha256": packet.get("skin_sha256"),
                 "scene_review_enabled": packet.get("scene_review_enabled", False),
                "reused_pending_packet": reused,
                "unread_chunk_indices": unread,
                "next_chunk_index": unread[0] if unread else None,
                "all_chunks_read": not unread,
                "first_chunk_included": False}

    def _hook_ledger(self, path: Path, meta: dict) -> str:
        text = (self._read(path, "hooks.md")
                if (path / "hooks.md").exists() else story_hooks.HEADER)
        need(not meta.get("hook_file_hash") or
             digest(text) == meta["hook_file_hash"],
             "HOOK_LEDGER_CHANGED", "Story memory changed outside an authorized transaction")
        return text

    def _first_seen(self, path: Path, meta: dict) -> dict[str, int]:
        if "first_seen" in meta:
            return dict(meta["first_seen"])
        return self._first_seen_from_archive(self._active(path))

    @staticmethod
    def _first_seen_from_archive(active: list[dict]) -> dict[str, int]:
        first_seen = {}
        for row in active:
            for name in row.get("participants", []):
                first_seen.setdefault(name, row["turn"])
        return first_seen

    @staticmethod
    def _author_windows(original: str, hooks: list[dict], turn: int) -> list[dict]:
        windows = story_hooks.author_window_candidates(original, turn)
        for item in windows:
            matches = [h["id"] for h in hooks
                       if h["window_end"] == item["window_end"] and
                       h["window_start"] == item["window_start"] and
                       (h["origin"] in item["source"] or item["source"] in h["origin"])]
            item["tracked_hook_ids"] = matches
            item["tracked"] = bool(matches)
            item["verification"] = "tracked" if matches else "needs_history_review"
            item.pop("overdue", None)
        return windows

    def director_brief(self, sid: str) -> dict:
        """READ BEFORE choosing participants; never alter game time or scenes.

        The brief exposes ALL unresolved arc and hook obligations plus the
        complete cast ID index. Actual character cards are loaded only later
        by prepare with relevant participants.
        """
        with self._locked(sid) as (path, meta):
            cast = self._cast(path)
            saved = self._hook_ledger(path, meta)
            hooks = story_hooks.parse(saved, cast)
            arcs_text = (self._read(path, "open_threads.md")
                         if (path / "open_threads.md").exists() else open_threads.HEADER)
            arcs = open_threads.parse(arcs_text)
            novel = self._read(path, "novel.md")
            original = self._read(path, "original.txt")
            need(digest(original) == meta["original_hash"], "ORIGINAL_CORRUPTED",
                 "Author original must remain intact for plot planning")
            story_hooks.validate_links(hooks, arcs)
            windows = self._author_windows(original, hooks, meta["turn"])
            display = {}
            for line in novel.splitlines():
                match = re.match(r"^- ([a-z][a-z0-9_]{1,49}) — (.+)$", line)
                if match and match.group(1) in cast:
                    display[match.group(1)] = match.group(2)
            first_seen = self._first_seen(path, meta)
            state_text = self._read(path, "state.md")
            state = json.loads(state_text)
            overview = story_hooks.director_brief(hooks, meta["turn"], first_seen,
                                                state["datetime"])
            valid, priority = story_hooks.plan_ids(overview, arcs, windows)
            atomic(path / "system/meta.json", dump({
                **meta, "first_seen": first_seen, "director_version": meta["version"],
                "director_hook_hash": digest(saved),
                "director_arc_hash": digest(arcs_text),
                "director_state_hash": digest(state_text),
                "director_valid_ids": sorted(valid),
                "director_priority_ids": sorted(priority),
            }))
            # Compact completed facts are available BEFORE the plan as well as
            # in the writing packet. Full evidence stays in the ledger.
            overview["unresolved_hooks"] = [
                {k: v for k, v in row.items() if k not in {"origin", "last_evidence"}}
                for row in overview["unresolved_hooks"]]
            overview["resolved_hooks"] = [
                {k: row[k] for k in ("id", "arc_id", "title", "kind", "detail",
                                     "last_turn", "deadline_state")}
                for row in overview["resolved_hooks"]]
            return {
                "session_id": sid, "version": meta["version"],
                "turn": meta["turn"],
                "state": state,
                "first_recorded_participation": first_seen,
                "plan_required": bool(valid),
                "plan_priority_ids": sorted(priority),
                "author_window_candidates": windows,
                "untracked_author_windows": [{"source_line": w["source_line"],
                                              "window_end": w["window_end"]}
                                             for w in windows if not w["tracked"]],
                "cast": [
                    {"id": c, "display": display.get(c, c)} for c in sorted(cast)
                ], "open_arcs": arcs,
                **overview,
                "empty_ledger": not hooks,
                "instructions": ("Use this BEFORE choosing participants. "
                                 "Check all author deadlines and ongoing NPC goals. "
                                 "Do not force an arbitrary appearance or give "
                                 "director-only information to POV/NPC. For an "
                                 "entering NPC, load its FULL card via prepare."),
            }

    def backfill_hooks(self, sid: str, expected_version: int,
                       additions: list[dict], arc_repairs: list[dict] | None = None) -> dict:
        """Safe explicit migration of EXISTING sessions from exact old sources.

        Explicit arc_repairs can restore missing arcs. Does not alter approved canon, state, history or any
        relationship. The caller must provide real canonical/archived quotes.
        A new version invalidates any prepared packet; reprepare afterward.
        """
        with self._locked(sid) as (path, meta):
            self._version(meta, expected_version)
            need(meta["approved"], "NOT_APPROVED", "Approve canon before backfill")
            arc_repairs = [] if arc_repairs is None else arc_repairs
            need(isinstance(additions, list) and isinstance(arc_repairs, list) and
                 bool(additions or arc_repairs),
                 "HOOK_BACKFILL_EMPTY", "Supply proven hooks or explicit historical arc repairs")
            active = self._active(path)
            last = meta["turn"]
            # The most recently saved turn may still be rolled back; do not
            # pin new permanent memory to an unstable source.
            for item in additions + arc_repairs:
                need(isinstance(item, dict) and
                     (not meta.get("last_undo") or last == 0 or
                      (item.get("origin_turn") != last and item.get("last_turn") != last)),
                     "HOOK_BACKFILL_LAST_TURN",
                     "Backfill from earlier scenes or the author original; "
                     "the latest turn can still be rolled back")
            original = self._read(path, "original.txt")
            need(digest(original) == meta["original_hash"], "ORIGINAL_CORRUPTED",
                 "Original canon must be intact before backfill")
            old = self._hook_ledger(path, meta)
            updated = story_hooks.backfill(
                old, additions, original,
                {row["turn"]: row["narration"] for row in active},
                last, self._cast(path))
            arcs_before = (self._read(path, "open_threads.md")
                           if (path / "open_threads.md").exists() else open_threads.HEADER)
            scenes = {row["turn"]: row["narration"] for row in active}
            arcs_after = open_threads.backfill(arcs_before, arc_repairs, original, scenes, last)
            story_hooks.validate_links(story_hooks.parse(updated), open_threads.parse(arcs_after))
            need(updated != old or arcs_after != arcs_before, "HOOK_BACKFILL_UNCHANGED",
                 "No new hooks or arc repairs supplied")
            newer = {**meta, "version": meta["version"] + 1,
                     "packet": None, "scene_review": None,
                     "hook_file_hash": digest(updated)}
            if meta.get("last_undo"):
                undo = deepcopy(meta["last_undo"])
                prior_cast = self._cast(path) - {Path(n).stem for n in
                    undo.get("created_files", []) if n.startswith("characters/")}
                prior = undo["before"].get("hooks.md", old)
                undo["before"]["hooks.md"] = story_hooks.backfill(
                    prior, additions, original,
                    {row["turn"]: row["narration"] for row in active
                     if row["turn"] < last}, last - 1, prior_cast)
                undo["before"]["open_threads.md"] = open_threads.backfill(
                    undo["before"].get("open_threads.md", arcs_before),
                    arc_repairs, original,
                    {row["turn"]: row["narration"] for row in active if row["turn"] < last},
                    last - 1)
                story_hooks.validate_links(story_hooks.parse(undo["before"]["hooks.md"], prior_cast),
                                           open_threads.parse(undo["before"]["open_threads.md"]))
                newer["last_undo"] = undo
            by_turn = {row["turn"]: row for row in active}
            for h in story_hooks.parse(updated, self._cast(path)):
                if h["kind"] == "appearance" and h["status"] == "closed":
                    need(bool(h["participants"]) and set(h["participants"]) <= set(
                        by_turn.get(h["last_turn"], {}).get("participants", [])),
                        "HOOK_APPEARANCE_EVIDENCE",
                        "Completed appearance requires actual archived participation")
            audit_log = self._read(path, "system/audit.jsonl")
            marker = dump({
                "kind": "hook_backfill", "turn": last,
                "added": additions,
                "arc_repairs": arc_repairs,
                "ledger_sha256": digest(updated),
            }) + "\n"
            self._write(path, meta, {
                "hooks.md": updated,
                "open_threads.md": arcs_after,
                "system/audit.jsonl": audit_log + marker,
            }, newer)
            return {"updated": True, "session_id": sid,
                    "version": newer["version"],
                    "added": len(additions),
                    "arc_repairs": len(arc_repairs),
                    "unresolved": sum(h["status"] != "closed"
                                      for h in story_hooks.parse(updated))}

    def prepare(self, sid: str, reader_text: str = "",
                participants: list[str] | None = None, kind: str = "turn",
                request_id: str | None = None,
                story_plan: dict | None = None,
                new_characters: list[dict] | None = None) -> dict:
        need(kind in ("turn", "audit", "canon"), "CONTEXT_KIND", "Unsupported context kind")
        need(isinstance(reader_text, str), "INPUT_TEXT", "Reader input must be text")
        need(request_id is None or (isinstance(request_id, str) and
             1 <= len(request_id) <= 100), "REQUEST_ID", "Invalid request id")
        with self._locked(sid) as (path, meta):
            if kind == "turn" and request_id is not None and _seen_request(meta, request_id):
                old, reverted = _archived_request(path, request_id)
                if old is not None:
                    need(not reverted and
                         not meta.get("requests", {}).get(request_id, {}).get("reverted"),
                         "REQUEST_REUSED",
                         "This operation was rolled back; choose a fresh request id")
                    need(old["reader_text"] == reader_text, "REQUEST_REUSED",
                         "Request id cannot be reused for different reader input")
                    return {"already_committed_duplicate": True, "saved": True,
                            "request_id": request_id, "version": meta["version"],
                            "turn": old["turn"], "narration": old["narration"],
                            "instruction": "Return the saved scene. Do not prepare or generate another."}
            if kind != "canon":
                need(meta["approved"], "NOT_APPROVED", "Confirm canon before writing")
                need((kind == "audit") == bool(meta["audit_due"]), "AUDIT_GATE",
                     "Audit is required at turn 15/60 before another scene")
            cast = self._cast(path)
            ledger = self._hook_ledger(path, meta)
            state = json.loads(self._read(path, "state.md"))
            if kind == "turn":
                arcs_text = (self._read(path, "open_threads.md")
                             if (path / "open_threads.md").exists() else open_threads.HEADER)
                open_threads.parse(arcs_text)
                need(meta.get("director_version") == meta["version"] and
                     meta.get("director_hook_hash") == digest(ledger) and
                     meta.get("director_arc_hash") == digest(arcs_text) and
                     meta.get("director_state_hash") == digest(self._read(path, "state.md")),
                     "DIRECTOR_REQUIRED", "Read director brief before preparing a new scene")
                validate_state(state, cast, meta["turn"])
                need(state["location"] != "НЕ ЗАДАНА", "OPENING_MISSING",
                     "Set opening scene before playing")
            pending = meta.get("packet")
            if (kind == "turn" and new_characters is None and pending
                    and pending.get("version") == meta["version"]
                    and pending.get("request_id") == request_id
                    and pending.get("reader_text") == reader_text):
                new_characters = list(pending.get("new_characters", {}).values())
            proposals = self._character_proposals(new_characters, cast)
            need(kind == "turn" or not proposals, "CHARACTER_PROPOSALS",
                 "New NPC proposals belong only to game turns")
            given = participants or []
            need(isinstance(given, list) and all(isinstance(p, str) for p in given)
                 and len(given) == len(set(given)), "PARTICIPANTS",
                 "Participants must be unique English names")
            relevant = set(given) | set(state["present"]) | set(state["remote"])
            if kind == "canon":
                relevant = cast
            elif kind == "audit":
                for row in _recent_turns(path, 15):
                    relevant.update(row.get("participants", []))
                if meta["audit_due"] == 60:
                    relevant = cast
            need(relevant <= cast | set(proposals), "UNKNOWN_CHARACTER", "Character not registered in novel or prepared as a proposal")
            need(set(proposals) <= relevant, "CHARACTER_PROPOSALS", "New NPC proposals must be prepared participants")
            signature = self._source_signature(path, relevant - set(proposals), kind)
            rules = self._read(self.instructions, "rules.md")
            skin = self._read(self.instructions, "skin_builder.md") if kind == "turn" else None
            rules_sha = digest(rules)
            skin_sha = digest(skin) if skin is not None else None
            reminder = (self._read(self.instructions, "author_reminder.md")
                        if kind == "turn" and (self.instructions / "author_reminder.md").is_file()
                        else None)
            reminder_sha = digest(reminder) if reminder is not None else None
            pending = meta.get("packet")
            if kind == "turn":
                if (story_plan is None and pending and pending.get("version") == meta["version"]
                        and pending.get("request_id") == request_id
                        and pending.get("reader_text") == reader_text):
                    story_plan = pending.get("story_plan")
                story_plan = story_hooks.validate_plan(story_plan,
                    set(meta.get("director_valid_ids", [])), set(meta.get("director_priority_ids", [])))
            else:
                need(story_plan is None, "STORY_PLAN_KIND", "Plan belongs only to game turns")
            if (pending and pending["version"] == meta["version"] and
                pending["kind"] == kind and pending["reader_text"] == reader_text and
                pending["participants"] == sorted(relevant) and
                pending.get("request_id") == request_id and
                 pending.get("story_plan") == story_plan and
                 pending.get("new_characters", {}) == proposals and
                 (kind != "turn" or pending.get("director_version") == meta["version"]) and
                 (kind != "audit" or "audit_story_ids" in pending) and
                pending.get("source_signature") == signature and
                 pending.get("instructions_mode") == "rules_first_skin_last_per_turn" and
                 pending.get("rules_sha256") == rules_sha and
                 pending.get("skin_sha256") == skin_sha and
                 pending.get("author_reminder_sha256") == reminder_sha and
                 pending.get("scene_review_enabled") == self.scene_review_enabled and
                pending.get("packet_stamp") == self._packet_stamp(path)):
                return self._manifest(pending, meta["audit_due"], reused=True)
            if kind == "canon":
                files = {"original.txt": self._read(path, "original.txt"),
                         "novel.md": self._read(path, "novel.md"),
                         "relationships.md": self._read(path, "relationships.md"),
                         "open_threads.md": (self._read(path, "open_threads.md")
                             if (path / "open_threads.md").exists() else open_threads.HEADER),
                          "hooks.md": (self._read(path, "hooks.md")
                              if (path / "hooks.md").exists() else story_hooks.HEADER)}
                need(digest(files["original.txt"]) == meta["original_hash"],
                     "ORIGINAL_CORRUPTED", "Author's original source no longer matches")
            else:
                files = {"novel.md": self._read(path, "novel.md"),
                         "state.md": self._read(path, "state.md"),
                         "relationships.md": self._read(path, "relationships.md"),
                         "timeline.md": self._read(path, "timeline.md"),
                         "open_threads.md": (self._read(path, "open_threads.md")
                             if (path / "open_threads.md").exists() else open_threads.HEADER),
                          "hooks.md": (self._read(path, "hooks.md")
                              if (path / "hooks.md").exists() else story_hooks.HEADER)}
            # Normalize legacy two-section relationships for a safe transition.
            # New files always include a short rule and directed familiarity.
            normalized_rel = validate_relations(
                json.loads(files["relationships.md"]), cast)
            if meta.get("relation_rule_hash"):
                need(digest(normalized_rel["rules"]) == meta["relation_rule_hash"],
                     "RELATION_RULE_CHANGED",
                     "Approved relationship rule changed outside authorized setup")
            # New sessions pin the entire mutable relationship file. Legacy
            # sessions without this hash still load and normalize safely.
            if meta.get("relationship_file_hash"):
                need(digest(files["relationships.md"]) == meta["relationship_file_hash"],
                     "RELATION_FILE_CHANGED",
                     "Relationship file changed outside an authorized transaction")
            files["relationships.md"] = dump(normalized_rel)
            open_threads.parse(files["open_threads.md"])
            files["hooks.md"] = ledger
            story_hooks.parse(ledger, cast)
            if meta.get("novel_hash"):
                need(digest(files["novel.md"]) == meta["novel_hash"],
                     "CANON_CHANGED", "Main questionnaire changed outside the engine")
            for name in sorted(relevant):
                card_path = "characters/" + name + ".md"
                if name in proposals:
                    files[card_path] = CHARACTER_RULE + proposals[name]["established_text"]
                    files["knowledge/" + name + ".md"] = "# Новые знания: " + name + "\n"
                    continue
                files[card_path] = self._read(path, card_path)
                files["knowledge/" + name + ".md"] = self._read(path, "knowledge/" + name + ".md")
                if name in meta.get("card_hashes", {}):
                    need(digest(files[card_path]) == meta["card_hashes"][name],
                         "CANON_CHANGED", "A character card changed outside the engine")
            files["characters/pov.md"] = self._read(path, "characters/pov.md") \
                if (path / "characters/pov.md").exists() else ""
            if files["characters/pov.md"] and "pov" in meta.get("card_hashes", {}):
                need(digest(files["characters/pov.md"]) == meta["card_hashes"]["pov"],
                     "CANON_CHANGED", "POV card changed outside the engine")
            if kind != "canon":
                files["knowledge/pov.md"] = self._read(path, "knowledge/pov.md")
            recent = [] if kind == "canon" else _recent_turns(path, 15 if kind == "audit" else 3)
            hook_rows = story_hooks.parse(ledger, cast)
            arcs = open_threads.parse(files["open_threads.md"])
            story_hooks.validate_links(hook_rows, arcs)
            plot_health = story_hooks.director_brief(hook_rows, meta["turn"],
                self._first_seen(path, meta), state["datetime"])
            audit_windows = (self._author_windows(self._read(path, "original.txt"),
                hook_rows, meta["turn"]) if kind == "audit" else [])
            audit_ids = []
            if kind == "turn":
                # Keep full live hooks and author completions. Every ordinary
                # resolved fact remains visible via the compact outcome index.
                files["hooks.md"] = story_hooks.render([
                    h for h in hook_rows if h["status"] != "closed"
                    or h["window_end"] is not None or h["kind"] == "appearance"], cast)
            elif kind == "audit":
                review_rows, _ = story_hooks.audit_selection(
                    hook_rows, arcs, meta["turn"], meta["audit_due"],
                    meta.get("story_audit_cursor", 0))
                files["hooks.md"] = story_hooks.render(review_rows, cast)
                audit_ids = sorted({"hook:" + h["id"] for h in review_rows} |
                                   {"arc:" + a["id"] for a in arcs} |
                                   {w["id"] for w in audit_windows})
            # Do not alphabetically sort this transport object: reading order matters.
            ordered_recent = [{"turn": row["turn"], "reader_text": row["reader_text"],
                               "narration": row["narration"], **row} for row in recent]
            context = {"rules": {"sha256": rules_sha, "chars": len(rules), "content": rules},
                       "recent_scenes": ordered_recent,
                       "kind": kind, "reader_text": reader_text,
                       "audit_due": meta["audit_due"], "participants": sorted(relevant),
                       "instructions_mode": "rules_first_skin_last_per_turn",
                       "files": files,
                       "new_character_proposals": list(proposals.values()),
                            "story_plan": story_plan,
                            "resolved_outcomes": story_hooks.outcome_index(hook_rows)
                                if kind == "turn" else [],
                            "audit_author_windows": audit_windows,
                            "audit_story_ids": audit_ids,
                            "audit_first_recorded_participation": self._first_seen(path, meta)
                                if kind == "audit" else {},
                            "story_health": {
                                "overdue_hook_ids": plot_health["author_deadlines_overdue"],
                                "active_author_window_ids": plot_health["author_windows_active"],
                                "appearance_review_ids": plot_health["appearance_review"],
                                "conditions_due_ids": plot_health["conditions_due"],
                                "conditions_review_ids": plot_health["conditions_review"],
                                "stale_hook_ids": plot_health["stale_hooks"]}}
            if reminder is not None:
                # A literary reminder, not a review gate; Skin Builder remains last.
                context["author_reminder"] = reminder
            payload = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
            skin_chunk_start = None
            if skin is not None:
                # Start a dedicated final block; no world data follows artistic text.
                prefix = payload[:-1]
                final_section = ',"skin_builder":' + json.dumps({
                    "sha256": skin_sha, "chars": len(skin), "empty": not bool(skin.strip()),
                    "scene_review_enabled": self.scene_review_enabled,
                    **({"review_sections": scene_preflight.review_sections(skin),
                        "review_rule_ids": [x["id"] for x in scene_preflight.review_items(skin)]}
                       if self.scene_review_enabled else {}),
                    "content": skin,
                }, ensure_ascii=False, separators=(",", ":")) + "}"
                content_chunks = chunks(prefix)
                skin_chunk_start = len(content_chunks)
                content_chunks.extend(chunks(final_section))
                payload = prefix + final_section
            else:
                content_chunks = chunks(payload)
            offsets = []
            position = 0
            for piece in content_chunks:
                size = len(piece.encode("utf-8"))
                offsets.append([position, size])
                position += size
            pid = secrets.token_hex(16)
            packet = {"id": pid, "version": meta["version"], "kind": kind,
                      "reader_text": reader_text, "participants": sorted(relevant),
                      "request_id": request_id, "source_signature": signature,
                      "director_version": meta["version"] if kind == "turn" else None,
                      "story_plan": story_plan, "audit_story_ids": audit_ids,
                      "new_characters": proposals,
                      "instructions_mode": "rules_first_skin_last_per_turn",
                      "rules_sha256": rules_sha, "skin_sha256": skin_sha,
                      "author_reminder_sha256": reminder_sha,
                      "skin_chunk_start": skin_chunk_start,
                      "scene_review_enabled": self.scene_review_enabled,
                      "hashes": [digest(c) for c in content_chunks], "received": [],
                      "offsets": offsets, "sha256": digest(payload)}
            atomic(path / "system/packet.json", payload)
            packet["packet_stamp"] = self._packet_stamp(path)
            atomic(path / "system/meta.json", dump({**meta, "packet": packet}))
            return self._manifest(packet, meta["audit_due"])

    def read_chunk(self, sid: str, packet_id: str, index: int) -> dict:
        with self._locked(sid) as (path, meta):
            packet = meta.get("packet")
            need(packet is not None and packet["id"] == packet_id and
                 packet["version"] == meta["version"], "PACKET_EXPIRED",
                 "Prepare fresh context only if this packet actually expired")
            need(type(index) is int and 0 <= index < len(packet["offsets"]),
                 "CHUNK_INDEX", "Invalid chunk index")
            need(packet.get("instructions_mode") == "rules_first_skin_last_per_turn",
                 "PACKET_EXPIRED", "Prepare fresh context in the new reading order")
            unread_before = [i for i in range(len(packet["hashes"])) if i not in packet["received"]]
            need(index in packet["received"] or (unread_before and index == unread_before[0]),
                 "CHUNK_ORDER", "Read the next unread chunk in order; Skin Builder is last")
            if index == packet.get("skin_chunk_start"):
                need(digest(self._read(self.instructions, "skin_builder.md")) == packet["skin_sha256"],
                     "SKIN_VERSION_CHANGED", "Skin Builder changed; prepare fresh context")
            offset, length = packet["offsets"][index]
            with (path / "system/packet.json").open("rb") as handle:
                handle.seek(offset)
                fragment_bytes = handle.read(length)
            need(len(fragment_bytes) == length and
                 sha256(fragment_bytes).hexdigest() == packet["hashes"][index],
                 "CHUNK_HASH", "Stored context packet has changed")
            fragment = fragment_bytes.decode("utf-8")
            if index not in packet["received"]:
                packet["received"].append(index)
                atomic(path / "system/meta.json", dump(meta))
            unread = [i for i in range(len(packet["hashes"]))
                      if i not in packet["received"]]
            return {"index": index, "content": fragment,
                     "phase": ("skin_builder" if packet.get("skin_chunk_start") is not None
                               and index >= packet["skin_chunk_start"] else "context"),
                    "sha256": packet["hashes"][index],
                    "chunk_count": len(packet["hashes"]), "packet_id": packet_id,
                    "next_chunk_index": unread[0] if unread else None,
                    "unread_chunk_indices": unread,
                    "all_chunks_read": not unread}

    @staticmethod
    def _receipt(meta: dict, packet_id: str, hashes: list[str], kind: str) -> dict:
        p = meta.get("packet")
        need(p is not None and p["id"] == packet_id and
             p["version"] == meta["version"] and p["kind"] == kind,
             "PACKET_EXPIRED", "Use the current prepared context")
        need(p.get("instructions_mode") == "rules_first_skin_last_per_turn",
             "PACKET_EXPIRED", "Prepare fresh context in the new reading order")
        need(hashes == p["hashes"] and
             set(p["received"]) == set(range(len(p["hashes"]))),
             "PACKET_INCOMPLETE", "Read and verify all 36000-character chunks")
        return p

    @staticmethod
    def _apply_relations(current: dict, changes: list[dict], cast: set[str],
                         participants: set[str], narration: str) -> dict:
        try:
            return relationship_store.update(current, changes, cast, participants, narration)
        except relationship_store.RelationProblem as exc:
            raise EngineError("RELATION_INVALID", str(exc), 422) from exc

    def scene_review_manifest(self) -> dict:
        """Inspect the retained optional reviewer; do not invoke during disabled turns."""
        skin = self._read(self.instructions, "skin_builder.md")
        return {
            "skin_sha256": digest(skin),
            "scene_review_enabled": self.scene_review_enabled,
            "sections": scene_preflight.review_sections(skin),
            "rules": scene_preflight.review_items(skin),
        }

    def review_scene(self, sid: str, expected_version: int, packet_id: str,
                     request_id: str, skin_sha256: str, narration: str,
                     checks: list[dict], rule_checks: list[dict] | None = None) -> dict:
        """Check the FULL current builder before committing a scene.

        This does NOT generate or repair prose. The model supplies a per-section
        literary audit; the server checks its coverage, objective layout and
        binding to the exact narration/packet/builder. Failed reviews save
        nothing. A successful receipt is required by commit for authored format.
        """
        with self._locked(sid) as (path, meta):
            self._version(meta, expected_version)
            packet = meta.get("packet")
            need(packet is not None and packet.get("id") == packet_id,
                 "PACKET_EXPIRED", "Review only the current prepared packet")
            self._receipt(meta, packet_id, packet["hashes"], "turn")
            need(packet.get("scene_review_enabled") == self.scene_review_enabled,
                 "REVIEW_MODE_CHANGED", "Prepare fresh context for the current review mode")
            if not self.scene_review_enabled:
                return {"scene_review_enabled": False, "skipped": True,
                        "approved": False, "issues": [],
                        "instruction": "Formal literary review is disabled. Save final narration directly."}
            need(packet.get("request_id") in (None, request_id),
                 "REQUEST_ID_MISMATCH", "Review request must match prepared request")
            need(isinstance(narration, str) and 0 < len(narration) <= 120_000,
                 "SCENE_TEXT", "Review requires the complete scene")
            skin = self._read(self.instructions, "skin_builder.md")
            actual_sha = digest(skin)
            need(skin_sha256 == actual_sha == packet.get("skin_sha256"), "SKIN_VERSION_CHANGED",
                 "Refresh the active Skin Builder: prepare and read the fresh packet")
            need(digest(self._read(self.instructions, "rules.md")) == packet.get("rules_sha256"),
                 "RULES_VERSION_CHANGED", "Rules changed; prepare and read the fresh packet")
            sections = scene_preflight.review_sections(skin)
            problems = (scene_preflight.layout_errors(skin, narration, meta["turn"] + 1)
                        + scene_preflight.report_errors(skin, checks)
                        + scene_preflight.rule_report_errors(skin, narration, rule_checks))
            if problems:
                if meta.get("scene_review") is not None:
                    atomic(path / "system/meta.json", dump({**meta, "scene_review": None}))
                return {"approved": False, "issues": problems,
                        "required_sections": sections,
                        "required_rule_ids": [x["id"] for x in scene_preflight.review_items(skin)],
                        "skin_sha256": actual_sha}
            receipt = {"request_id": request_id, "packet_id": packet_id,
                       "narration_sha256": digest(narration),
                       "skin_sha256": actual_sha, "version": meta["version"]}
            atomic(path / "system/meta.json", dump({**meta, "scene_review": receipt}))
            return {"approved": True, "issues": [], "required_sections": sections,
                    "skin_sha256": actual_sha, "narration_sha256": digest(narration)}

    def commit(self, sid: str, data: dict) -> dict:
        required = {"request_id", "expected_version", "packet_id", "chunk_hashes",
                    "reader_text", "narration", "participants", "state",
                    "knowledge", "events", "relations"}
        need(isinstance(data, dict) and required <= set(data) and
             set(data) <= required | {"thread_updates", "hook_updates", "scene_access",
                                       "new_character_evidence"},
             "TURN_FIELDS", "Turn payload has fixed storage fields")
        req = data["request_id"]
        need(isinstance(req, str) and 1 <= len(req) <= 100, "REQUEST_ID", "Invalid request id")
        request_hash = digest(dump(data))
        with self._locked(sid) as (path, meta):
            prior = meta["requests"].get(req)
            if prior:
                need(prior["hash"] == request_hash and not prior.get("reverted"),
                     "REQUEST_REUSED", "Request id was reused or rolled back")
                stored = _archived_turn(path, req)
                need(stored is not None, "HISTORY_INTEGRITY",
                     "Saved request is missing from the scene archive")
                return {**prior["result"], "narration": stored["narration"]}
            if _seen_request(meta, req):
                old, reverted = _archived_request(path, req)
                if old is not None:
                    need(not reverted and old.get("request_hash") == request_hash,
                         "REQUEST_REUSED",
                         "Old request id was reused, changed, or rolled back")
                    result = old.get("saved_result")
                    need(isinstance(result, dict), "REQUEST_REUSED",
                         "Old legacy request cannot be safely replayed")
                    return {**result, "narration": old["narration"]}
            self._version(meta, data["expected_version"])
            need(meta["approved"] and not meta["audit_due"], "AUDIT_GATE",
                 "Approval or audit required")
            packet = self._receipt(meta, data["packet_id"], data["chunk_hashes"], "turn")
            need(packet.get("director_version") == meta["version"], "DIRECTOR_REQUIRED",
                 "Prepare a new director-bound packet before saving")
            need(packet.get("packet_stamp") == self._packet_stamp(path),
                 "PACKET_CHANGED", "Prepared context bytes changed before commit")
            need(packet.get("request_id") is None or
                 packet["request_id"] == req, "REQUEST_ID_MISMATCH",
                 "Prepared request ID and committed request ID differ")
            need(packet.get("source_signature") ==
                 self._source_signature(path, set(packet["participants"]) -
                                         set(packet.get("new_characters", {})), "turn"),
                 "SOURCE_CHANGED", "A file changed after preparing the context")
            need(packet["reader_text"] == data["reader_text"],
                 "INPUT_MISMATCH", "Prepared and committed reader text differ")
            # Full authored Skin Builder review is a mandatory pre-save gate.
            # Synthetic fixtures without an authored format remain compatible.
            skin = self._read(self.instructions, "skin_builder.md")
            need(digest(skin) == packet.get("skin_sha256"), "SKIN_VERSION_CHANGED",
                 "Refresh the active Skin Builder: prepare and read the fresh packet")
            need(digest(self._read(self.instructions, "rules.md")) == packet.get("rules_sha256"),
                 "RULES_VERSION_CHANGED", "Rules changed; prepare and read the fresh packet")
            need(packet.get("scene_review_enabled") == self.scene_review_enabled,
                 "REVIEW_MODE_CHANGED", "Prepare fresh context for the current review mode")
            if self.scene_review_enabled and (scene_preflight.is_formatted_builder(skin)
                                               or packet.get("new_characters")):
                receipt = meta.get("scene_review") or {}
                need(receipt.get("request_id") == req
                     and receipt.get("packet_id") == data["packet_id"]
                     and receipt.get("version") == meta["version"]
                     and receipt.get("narration_sha256") == digest(data["narration"])
                     and receipt.get("skin_sha256") == digest(skin),
                     "SCENE_REVIEW_REQUIRED",
                     "Review the exact final scene against every current Skin Builder "
                     "section before saving")

            registered_cast = self._cast(path)
            proposals = self._character_proposals(
                list(packet.get("new_characters", {}).values()), registered_cast)
            used = data["participants"]
            need(isinstance(used, list) and len(used) == len(set(used)) and
                 set(used) <= set(packet["participants"]), "UNLOADED_CHARACTER",
                 "Load any entering character before writing")
            need(isinstance(data["narration"], str) and bool(data["narration"].strip())
                 and len(data["narration"]) <= 120_000, "SCENE_TEXT", "Invalid full scene")
            # Persist only proposals actually used in the final saved scene.
            new_npcs = {name: dict(row) for name, row in proposals.items() if name in used}
            final_evidence = data.get("new_character_evidence", {})
            need(isinstance(final_evidence, dict) and set(final_evidence) <= set(new_npcs)
                 and all(isinstance(quote, str) and 8 <= len(quote.strip()) <= 4000
                         for quote in final_evidence.values()),
                 "CHARACTER_SOURCE", "Final NPC evidence belongs only to used temporary NPCs")
            for name, quote in final_evidence.items():
                new_npcs[name]["established_text"] = quote
            need(all(row["established_text"] in data["narration"] for row in new_npcs.values()),
                 "CHARACTER_SOURCE", "New NPC card requires an exact excerpt from the final approved scene")
            cast = registered_cast | set(new_npcs)
            state = validate_state(data["state"], cast, meta["turn"] + 1)
            need((set(state["present"]) | set(state["remote"])) <= set(used),
                 "STATE_UNLOADED", "All present/remote characters must be loaded")
            before_state = json.loads(self._read(path, "state.md"))
            validate_state(before_state, registered_cast, meta["turn"])
            # A loaded card or presence in an earlier scene is NOT proof
            # of witnessing this one: the new turn may jump in time/place.
            # Anyone who left by the final state needs a literal witness
            # receipt for their actual participation earlier in this turn.
            present_during_turn = set(state["present"]) | set(state["remote"])
            access = data.get("scene_access", [])
            need(isinstance(access, list) and len(access) <= len(used),
                 "SCENE_ACCESS", "Scene access receipts must be short")
            access_ids: set[str] = set()
            for item in access:
                need(isinstance(item, dict) and
                     set(item) == {"character", "channel", "source"},
                     "SCENE_ACCESS", "Witness receipt needs character/channel/source")
                witness = item["character"]
                need(witness in used and witness not in access_ids and
                     item["channel"] in {"physical", "remote", "ability"} and
                     isinstance(item["source"], str) and
                     8 <= len(item["source"]) <= 260 and
                     "\n" not in item["source"] and
                     item["source"] in data["narration"],
                     "SCENE_ACCESS", "Access must be supported by a direct scene excerpt")
                access_ids.add(witness)
            need(set(used) <= present_during_turn | access_ids,
                 "PHANTOM_PARTICIPANT",
                 "A loaded character without actual scene access cannot gain knowledge or feelings")
            if before_state["datetime"] and state["datetime"]:
                try:
                    need(datetime.fromisoformat(state["datetime"]) >=
                         datetime.fromisoformat(before_state["datetime"]),
                         "TIME_REVERSED", "World time cannot move backwards")
                except TypeError:
                    raise EngineError("TIME_FORMAT", "Use compatible datetime offsets", 422)
            before_rel = self._read(path, "relationships.md")
            if meta.get("relationship_file_hash"):
                need(digest(before_rel) == meta["relationship_file_hash"],
                     "RELATION_FILE_CHANGED", "Relations changed outside authorized gameplay")
            if meta.get("relation_rule_hash"):
                current_rel = validate_relations(json.loads(before_rel), cast)
                need(digest(current_rel["rules"]) == meta["relation_rule_hash"],
                     "RELATION_RULE_CHANGED", "Approved relationship rule changed")
            before_timeline = self._read(path, "timeline.md")
            before_threads = (self._read(path, "open_threads.md")
                              if (path / "open_threads.md").exists() else open_threads.HEADER)
            after_threads, thread_changes = open_threads.apply(
                before_threads, data.get("thread_updates", []),
                data["narration"], meta["turn"] + 1)
            before_hooks = self._hook_ledger(path, meta)
            after_hooks, hook_changes = story_hooks.apply(
                before_hooks, data.get("hook_updates", []),
                data["narration"], meta["turn"] + 1, cast)
            closed_arcs = {item["id"] for item in thread_changes
                           if item["status"] == "closed"}
            remaining_hooks = [h for h in story_hooks.parse(after_hooks, cast)
                               if h["status"] != "closed"]
            for h in hook_changes:
                if h["status"] == "closed" and h["kind"] == "appearance":
                    need(bool(h["participants"]) and set(h["participants"]) <= set(used),
                         "HOOK_APPEARANCE_EVIDENCE",
                         "Appearance closure requires actual scene participation")
            need(not any(h["arc_id"] in closed_arcs for h in remaining_hooks),
                 "ARC_HAS_OPEN_HOOKS",
                 "An arc cannot close while independent unresolved hooks "
                 "remain. Resolve the surviving hooks with evidence or leave the arc open.")
            story_hooks.validate_links(story_hooks.parse(after_hooks, cast),
                                       open_threads.parse(after_threads))
            need(isinstance(data["relations"], list), "RELATION_CHANGES", "Relations must be a list")
            for change in data["relations"]:
                if isinstance(change, dict):
                    need(change.get("source") == "pov" or change.get("source") in used,
                         "UNLOADED_RELATION_OWNER",
                         "Load the source character before changing their relationships")
            after_rel = dump(self._apply_relations(
                json.loads(before_rel), data["relations"], cast, set(used),
                data["narration"]))
            files = {"state.md": dump(state)}
            if after_rel != before_rel:
                files["relationships.md"] = after_rel
            if after_threads != before_threads:
                files["open_threads.md"] = after_threads
            if after_hooks != before_hooks:
                files["hooks.md"] = after_hooks
            before = {"state.md": self._read(path, "state.md"),
                      "relationships.md": before_rel, "timeline.md": before_timeline,
                      "open_threads.md": before_threads,
                      "hooks.md": before_hooks}
            created_files = []
            if new_npcs:
                old_novel = self._read(path, "novel.md")
                need(not meta.get("novel_hash") or digest(old_novel) == meta["novel_hash"],
                     "CANON_CHANGED", "Main questionnaire changed outside the engine")
                before["novel.md"] = old_novel
                files["novel.md"] = self._with_new_cast(old_novel, new_npcs)
                for name, row in new_npcs.items():
                    card, memory = "characters/" + name + ".md", "knowledge/" + name + ".md"
                    need(not (path / card).exists() and not (path / memory).exists(),
                         "CHARACTER_EXISTS", "New NPC files already exist")
                    files[card] = CHARACTER_RULE + row["established_text"]
                    files[memory] = "# Новые знания: " + name + "\n"
                    created_files.extend([card, memory])
            need(isinstance(data["knowledge"], list), "KNOWLEDGE_TYPE",
                 "Knowledge entries must be a list")
            knowledge_rows: list[dict] = []
            for entry in data["knowledge"]:
                need(isinstance(entry, dict) and set(entry) == {"owner", "fact", "source"},
                     "KNOWLEDGE_FIELDS", "Only owner, fact and source")
                owner, fact, source = entry["owner"], entry["fact"], entry["source"]
                need(owner == "pov" or (owner in cast and owner in used),
                     "KNOWLEDGE_OWNER", "Knowledge only for a loaded participant or POV")
                need(isinstance(fact, str) and 0 < len(fact.strip()) <= 350 and
                     "\n" not in fact and "\r" not in fact, "KNOWLEDGE_FACT",
                     f"Knowledge fact for {owner} must be single-line (1–350 characters)")
                need(isinstance(source, str) and 3 <= len(source) <= 400,
                     "KNOWLEDGE_SOURCE", f"Knowledge source for {owner} must be 3–400 characters")
                need(source in data["narration"], "KNOWLEDGE_SOURCE",
                     f"Knowledge source for {owner} must be an exact excerpt from the saved scene")
                filename = "knowledge/" + owner + ".md"
                current = files.get(filename)
                if current is None:
                    current = self._read(path, filename)
                    before[filename] = current
                marker = knowledge_marker(fact, source)
                need(not any(row.startswith("- " + fact.strip() + " [источник:")
                             for row in current.splitlines()),
                     "KNOWLEDGE_DUPLICATE", "Fact already recorded for this character")
                files[filename] = current + "\nХод " + str(meta["turn"] + 1) + \
                                  " · " + str(state["datetime"] or "дата не задана") + \
                                  "\n" + marker + "\n"
                knowledge_rows.append(entry)
            need(isinstance(data["events"], list), "EVENTS_TYPE", "Events must be a list")
            timeline = before_timeline
            for event in data["events"]:
                need(isinstance(event, dict) and set(event) == {"text", "participants"},
                     "EVENT_FIELDS", "Timeline only needs text and participants")
                text = event["text"]
                names = event["participants"]
                need(isinstance(text, str) and 0 < len(text.strip()) <= 350 and
                     "\n" not in text and "\r" not in text and
                     isinstance(names, list) and all(isinstance(n, str) for n in names)
                     and len(names) == len(set(names)) and set(names) <= set(used) | {"pov"},
                     "EVENT_DATA", "Event must be a single-line milestone with known participants")
                need("\n" + text.strip() + "\n" not in timeline,
                     "EVENT_DUPLICATE", "Identical world milestone already recorded")
                timeline += ("\nХод " + str(meta["turn"] + 1) +
                             " · " + str(state["datetime"] or "дата не задана") + "\n" +
                             "Участники: " + ", ".join(names) + "\n" +
                             text.strip() + "\n")
            if timeline != before_timeline:
                files["timeline.md"] = timeline
            older_scenes = _recent_turns(path, 3)
            warnings = (["SCENE_NEAR_DUPLICATE"]
                        if _near_duplicate(data["narration"], older_scenes) else [])
            warnings.extend(_speech_knowledge_flags(
                files.get("novel.md", self._read(path, "novel.md")), data["narration"],
                json.loads(after_rel)))
            record = {"kind": "turn", "turn": meta["turn"] + 1, "request_id": req,
                      "reader_text": data["reader_text"], "narration": data["narration"],
                      "participants": list(used), "scene_access": access,
                      "knowledge": knowledge_rows,
                      "events": data["events"], "relations": data["relations"],
                      "thread_updates": thread_changes,
                      "hook_updates": hook_changes,
                      "new_characters": list(new_npcs.values()),
                      "story_plan": packet.get("story_plan"),
                      "warnings": warnings}
            due = 60 if (meta["turn"] + 1) % 60 == 0 else \
                  15 if (meta["turn"] + 1) % 15 == 0 else 0
            first_seen_before = self._first_seen(path, meta)
            first_seen_after = dict(first_seen_before)
            for name in used:
                first_seen_after.setdefault(name, meta["turn"] + 1)
            new = deepcopy(meta)
            new.update(version=meta["version"] + 1, turn=meta["turn"] + 1,
                       first_seen=first_seen_after,
                       scene_review=None, relationship_file_hash=digest(after_rel),
                        hook_file_hash=digest(after_hooks),
                       audit_due=due, packet=None,
                       last_undo={"request_id": req, "before": before,
                                  "created_files": created_files,
                                  "prior_turn": meta["turn"],
                                  "prior_audit_due": meta["audit_due"]})
            if new_npcs:
                new["novel_hash"] = digest(files["novel.md"])
                new["card_hashes"] = {**meta.get("card_hashes", {}), **{
                    name: digest(files["characters/" + name + ".md"]) for name in new_npcs}}
            # Director receipt belongs to the previous version; avoid retaining
            # large unused planning indexes in session metadata.
            for key in tuple(new):
                if key.startswith("director_"):
                    del new[key]
            result = {"saved": True, "session_id": sid, "turn": new["turn"],
                      "version": new["version"], "audit_due": due,
                      "narration": data["narration"], "warnings": warnings}
            new["requests"][req] = {"hash": request_hash,
                                    "result": {k: v for k, v in result.items()
                                               if k != "narration"}}
            record["request_hash"] = request_hash
            record["saved_result"] = new["requests"][req]["result"]
            _remember_request(new, req)
            self._write(path, meta, files, new,
                        appends={"history.jsonl": dump(record) + "\n"})
            return result

    def rollback(self, sid: str, expected_version: int,
                 request_id: str, reason: str) -> dict:
        with self._locked(sid) as (path, meta):
            self._version(meta, expected_version)
            undo = meta.get("last_undo")
            need(undo is not None and undo["request_id"] == request_id and
                 isinstance(reason, str) and reason.strip(), "ROLLBACK_LAST_ONLY",
                 "Rollback requires exact last turn request id and reason")
            files = deepcopy(undo["before"])
            created_files = undo.get("created_files", [])
            marker = dump({"kind": "rollback", "reverts": request_id,
                           "reason": reason, "turn": meta["turn"]}) + "\n"
            new = deepcopy(meta)
            new.update(version=meta["version"] + 1,
                       turn=undo["prior_turn"],
                       relationship_file_hash=digest(
                           files.get("relationships.md",
                                     self._read(path, "relationships.md"))),
                       audit_due=undo["prior_audit_due"],
                        hook_file_hash=digest(files.get("hooks.md", self._hook_ledger(path, meta))),
                       last_undo=None, packet=None)
            if "first_seen" in meta:
                new["first_seen"] = {name: turn for name, turn in meta["first_seen"].items()
                                     if turn <= undo["prior_turn"]}
            else:
                new.pop("first_seen", None)  # legacy undo; rebuild from active archive
            if created_files:
                new["novel_hash"] = digest(files["novel.md"])
                new["card_hashes"] = {name: checksum for name, checksum in
                    meta.get("card_hashes", {}).items()
                    if "characters/" + name + ".md" not in created_files}
            new["scene_review"] = None
            new["requests"][request_id]["reverted"] = True
            self._write(path, meta, files, new,
                        appends={"history.jsonl": marker}, deletes=created_files)
            return {"rolled_back": True, "turn": new["turn"],
                    "version": new["version"]}

    def audit(self, sid: str, expected_version: int, packet_id: str,
              chunk_hashes: list[str], report: dict[str, str],
              corrections: dict[str, dict] | None = None) -> dict:
        with self._locked(sid) as (path, meta):
            self._version(meta, expected_version)
            packet = self._receipt(meta, packet_id, chunk_hashes, "audit")
            need(packet.get("packet_stamp") == self._packet_stamp(path),
                 "PACKET_CHANGED", "Prepared audit context bytes changed")
            need(meta["audit_due"] in (15, 60), "AUDIT_NOT_DUE", "No audit due")
            need(digest(self._read(self.instructions, "rules.md")) == packet.get("rules_sha256"),
                 "RULES_VERSION_CHANGED", "Rules changed; prepare and read the fresh audit packet")
            required = {"continuity", "state", "knowledge", "timeline", "relationships"}
            if meta["audit_due"] == 60:
                required |= {"long_term"}
            story_cast = self._cast(path)
            hook_ledger = self._hook_ledger(path, meta)
            hook_rows = story_hooks.parse(hook_ledger, story_cast)
            arc_rows = open_threads.parse(
                self._read(path, "open_threads.md")
                if (path / "open_threads.md").exists() else open_threads.HEADER)
            story_hooks.validate_links(hook_rows, arc_rows)
            windows = self._author_windows(self._read(path, "original.txt"),
                                           hook_rows, meta["turn"])
            review_rows, next_audit_cursor = story_hooks.audit_selection(
                hook_rows, arc_rows, meta["turn"], meta["audit_due"],
                meta.get("story_audit_cursor", 0))
            expected = ({"hook:" + h["id"] for h in review_rows} |
                        {"arc:" + a["id"] for a in arc_rows} |
                        {w["id"] for w in windows})
            need(packet.get("audit_story_ids") == sorted(expected),
                 "AUDIT_PACKET_STALE", "Prepare current story audit context")
            if expected:
                required.add("story")
            need(isinstance(report, dict) and set(report) == required and all(
                 isinstance(report[k], str) and bool(report[k].strip())
                 for k in required - {"story"}),
                 "AUDIT_REPORT", "Audit must check required domains")
            if expected:
                actual = report["story"]
                need(isinstance(actual, dict) and set(actual) == expected and
                     all(isinstance(note, str) and len(note.strip()) >= 16
                         for note in actual.values()),
                     "AUDIT_STORY",
                     "Plot audit must address every unresolved hook and arc, "
                     "including missed author appearance windows")
            need(digest(self._read(path, "original.txt")) == meta["original_hash"],
                 "ORIGINAL_CORRUPTED", "Author's original changed; audit cannot continue")
            corrections = corrections or {}
            need(isinstance(corrections, dict), "AUDIT_CORRECTIONS", "Invalid corrections")
            changes: dict[str, str] = {}
            trail: list[dict] = []
            cast = self._cast(path)
            archived = self._active(path)
            if meta.get("relationship_file_hash"):
                need(digest(self._read(path, "relationships.md")) ==
                     meta["relationship_file_hash"],
                     "RELATION_FILE_CHANGED",
                     "Relationships changed outside authorized gameplay")
            if meta.get("relation_rule_hash"):
                current_rel = validate_relations(
                    json.loads(self._read(path, "relationships.md")), cast)
                need(digest(current_rel["rules"]) == meta["relation_rule_hash"],
                     "RELATION_RULE_CHANGED",
                     "Approved relationship rule changed before audit")
            for name, row in corrections.items():
                need(name in {"state.md", "relationships.md", "timeline.md"} or
                     (re.fullmatch(r"knowledge/[a-z][a-z0-9_]{1,49}\.md", name) and
                      (name == "knowledge/pov.md" or name[10:-3] in cast)),
                     "AUDIT_PATH", "Audit may only correct current state documents")
                keys = {"before_sha256", "after", "reason"}
                if name == "relationships.md" or (
                    name in {"timeline.md"} or name.startswith("knowledge/")) and (
                    isinstance(row, dict) and "evidence" in row):
                    keys.add("evidence")
                need(isinstance(row, dict) and set(row) == keys
                     and isinstance(row["after"], str) and isinstance(row["reason"], str)
                     and row["reason"].strip(), "AUDIT_CHANGE",
                     "Every correction needs a reason and relationship corrections need evidence")
                current = self._read(path, name)
                need(row["before_sha256"] == digest(current), "AUDIT_STALE",
                     "Audit correction is based on a different file version")
                after = row["after"]
                if name == "state.md":
                    validate_state(json.loads(after), cast, meta["turn"])
                elif name == "relationships.md":
                    updated = validate_relations(json.loads(after), cast)
                    prior = validate_relations(json.loads(current), cast)
                    need(updated["rules"] == prior["rules"], "RELATION_RULE_IMMUTABLE",
                         "Audit cannot alter author-approved relationship rules")
                    quote = row["evidence"]
                    need(isinstance(quote, str) and
                         8 <= len(quote) <= 260 and
                         (quote in self._read(path, "original.txt") or
                          any(quote in scene["narration"] for scene in archived)),
                         "AUDIT_RELATION_EVIDENCE",
                         "Relationship correction requires an exact original or saved-scene excerpt")
                elif name.startswith("knowledge/"):
                    need(after.startswith(current) and after != current,
                         "AUDIT_APPEND_ONLY",
                         "Audit must append knowledge corrections; it cannot erase prior facts")
                    quote = row.get("evidence")
                    owner = name[len("knowledge/"):-3]
                    need(isinstance(quote, str) and
                         8 <= len(quote) <= 260 and
                         quote in after[len(current):] and
                         any(quote in scene["narration"] and
                             (owner == "pov" or owner in scene["participants"])
                             for scene in archived),
                         "AUDIT_KNOWLEDGE_EVIDENCE",
                         "Appended personal knowledge needs an exact witnessed scene quote")
                elif name == "timeline.md":
                    need(after.startswith(current) and after != current,
                         "AUDIT_APPEND_ONLY",
                         "Audit must append timeline corrections; it cannot erase past events")
                    quote = row.get("evidence")
                    need(isinstance(quote, str) and
                         8 <= len(quote) <= 260 and
                         quote in after[len(current):] and
                         any(quote in scene["narration"] for scene in archived),
                         "AUDIT_TIMELINE_EVIDENCE",
                         "Appended timeline correction needs an exact scene quote")
                changes[name] = after
                trail.append({"file": name, "reason": row["reason"],
                              "before": current, "after": after})
            # Deterministically reconcile every committed fact/milestone with
            # its authoritative original scene archive. This catches loss or
            # accidental editing; semantic accuracy remains the writer's duty.
            open_threads.parse(self._read(path, "open_threads.md")
                               if (path / "open_threads.md").exists()
                               else open_threads.HEADER)
            scenes_by_turn = {row["turn"]: row["narration"] for row in archived}
            original = self._read(path, "original.txt")
            for hook in hook_rows:
                origin_text = (original if hook["origin_turn"] == 0
                               else scenes_by_turn.get(hook["origin_turn"], ""))
                latest_text = (original if hook["last_turn"] == 0
                               else scenes_by_turn.get(hook["last_turn"], ""))
                need(hook["origin"] in origin_text and
                     hook["last_evidence"] in latest_text,
                     "AUDIT_HOOK_PROVENANCE",
                     "Unresolved hook has no exact original or scene evidence")
            hook_ids = {h["id"] for h in hook_rows}
            existing_files: dict[str, str] = {}
            for old_turn in archived:
                for fact in old_turn.get("knowledge", []):
                    owner = fact["owner"]
                    need(fact["source"] in old_turn["narration"] and
                         (owner == "pov" or owner in old_turn["participants"]),
                         "AUDIT_KNOWLEDGE_PROVENANCE",
                         "Archived personal fact has no witnessed source in its own scene")
                    filename = "knowledge/" + owner + ".md"
                    if filename not in existing_files:
                        existing_files[filename] = changes.get(
                            filename, self._read(path, filename))
                    marker = knowledge_marker(fact["fact"], fact["source"])
                    need(marker in existing_files[filename],
                         "AUDIT_MISSING_KNOWLEDGE",
                         "A previously saved personal fact disappeared from " + filename)
                for relation in old_turn.get("relations", []):
                    src, dst = relation["source"], relation["target"]
                    need((src == "pov" or src in old_turn["participants"]) and
                         (dst == "pov" or dst in old_turn["participants"]),
                         "AUDIT_RELATION_PROVENANCE",
                         "Archived relationship update lacks involved participants")
                    if relation.get("evidence") is not None:
                        need(relation["evidence"] in old_turn["narration"],
                             "AUDIT_RELATION_PROVENANCE",
                             "Relationship change evidence is absent from its scene")
                for thread in old_turn.get("thread_updates", []):
                    need(thread["source"] in old_turn["narration"],
                         "AUDIT_THREAD_PROVENANCE",
                         "Thread change is not anchored to its saved scene")
                for hook in old_turn.get("hook_updates", []):
                    need(hook["id"] in hook_ids, "AUDIT_MISSING_HOOK",
                         "A previously recorded hook disappeared from persistent memory")
                    need(hook["source"] in old_turn["narration"],
                         "AUDIT_HOOK_PROVENANCE",
                         "Hook change is not anchored to its saved scene")
                if old_turn.get("events"):
                    if "timeline.md" not in existing_files:
                        existing_files["timeline.md"] = changes.get(
                            "timeline.md", self._read(path, "timeline.md"))
                    for event in old_turn["events"]:
                        need("\n" + event["text"].strip() + "\n" in
                             existing_files["timeline.md"],
                             "AUDIT_MISSING_EVENT",
                             "A previously saved world event disappeared from timeline.md")
            need(packet.get("source_signature") ==
                 self._source_signature(path, set(packet["participants"]), "audit"),
                 "SOURCE_CHANGED", "Audit source changed; prepare again")
            audit_log = self._read(path, "system/audit.jsonl")
            for line in audit_log.splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("kind") == "hook_backfill":
                    need(all(h["id"] in hook_ids for h in event["added"]),
                         "AUDIT_MISSING_HOOK", "A backfilled story hook disappeared")
            changes["system/audit.jsonl"] = audit_log + dump(
                {"turn": meta["turn"], "level": meta["audit_due"],
                 "report": report, "corrections": trail}) + "\n"
            new = {**meta, "version": meta["version"] + 1, "audit_due": 0,
                   "hook_file_hash": digest(hook_ledger),
                   "first_seen": self._first_seen_from_archive(archived),
                   "story_audit_cursor": next_audit_cursor,
                   "packet": None, "last_undo": None}
            if "relationships.md" in changes:
                new["relationship_file_hash"] = digest(changes["relationships.md"])
            self._write(path, meta, changes, new)
            audited_state = json.loads(changes.get("state.md", self._read(path, "state.md")))
            findings = story_hooks.director_brief(hook_rows, meta["turn"],
                new["first_seen"], audited_state["datetime"])
            return {"audited": True, "turn": meta["turn"],
                    "version": new["version"], "corrections": len(trail),
                    "plot_findings": {
                        "overdue": findings["author_deadlines_overdue"],
                        "stale": findings["stale_hooks"],
                        "appearance_review": findings["appearance_review"],
                        "conditions_due": findings["conditions_due"],
                        "conditions_review": findings["conditions_review"],
                        "reviewed_hook_count": len(review_rows),
                        "open_hook_count": sum(h["status"] != "closed" for h in hook_rows),
                        "open_arc_count": len(arc_rows)}}

    def history(self, sid: str, start: int = 1, end: int | None = None,
                query: str = "") -> list[dict]:
        with self._locked(sid) as (path, _):
            need(type(start) is int and start >= 1, "HISTORY_RANGE", "Invalid start")
            return [row for row in self._active(path)
                    if row["turn"] >= start and (end is None or row["turn"] <= end)
                    and (not query or query.casefold() in
                         (row["narration"] + "\n" + row["reader_text"]).casefold())]
