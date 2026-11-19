"""Persistent versioned ChatNovella sessions.

Narrative state lives as readable Markdown. A revision contains a complete
immutable snapshot; current.json changes atomically at successful commit.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
from datetime import datetime
import json
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterator

from chatnovella.scene_review import MAX_CORRECTIONS, REVIEW_CHECKS, inspect_scene
from chatnovella.semantic_review import (
    ReviewUnavailable, evaluate_semantic, validate_verdict as validate_semantic_verdict, SEMANTIC_KEYS,
)

STATE_FILES = (
    "state/scene.md", "state/locations.md", "state/roster.md",
    "state/arcs.md", "state/timeline.md", "state/items.md", "state/recent.md",
)
CHAR_FILES = (
    "core.md", "appearance.md", "voice.md", "personality.md",
    "past.md", "abilities.md", "knowledge.md", "relations.md",
)
ALLOWED_FIXED = set(STATE_FILES) | {"novel.md", "pov.md"}
CHAR_PATH = re.compile(
    r"^characters/(NPC_[A-Za-z0-9_-]{1,60})/"
    r"(core|appearance|voice|personality|past|abilities|knowledge|relations)\.md$"
)
SESSION_ID = re.compile(r"^CN_[a-f0-9]{32}$")
CHUNK_SIZE = 36_000  # Unicode characters; transport reports its unit
MAX_INPUT_FILE = 2_000_000
TIMELINE_FORMAT = re.compile(r"^(\d{2}\.\d{2}\.\d{4})\s*\|\s*(\S[^\n]*)$")
TIMELINE_HEADER = "# Хронология — строго приватно"
DATE_HEADING = re.compile(r"^## (\d{2}\.\d{2}\.\d{4})\s*$")


# Общие инструкции приложения: не копируются в файлы отдельных сессий.
# Отправляются в начале КАЖДОГО игрового пакета, до состояния и анкет НПС.
ENGINE_CONTEXT_FILES = (
    "engine/rules.md",
    "engine/director.md",
    "engine/npc_rules.md",
    "engine/world_rules.md",
    "engine/strict_prohibitions.md",
    "engine/pov_autonomy.md",
    "engine/skin_builder.md",
    "engine/scene_review.md",
)


class StateError(ValueError):
    """Invalid state change, missing resource or stale version."""


def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".tmp_", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _safe_change(path: str, text: str) -> None:
    if not isinstance(path, str) or (path not in ALLOWED_FIXED and not CHAR_PATH.fullmatch(path)):
        raise StateError(f"Недопустимый путь файла: {path!r}")
    if not isinstance(text, str) or len(text) > MAX_INPUT_FILE:
        raise StateError(f"Некорректное содержимое: {path!r}")


def _relation_stats(content: str, path: str, required: bool = False) -> dict[str, tuple[int, int]]:
    """Read NPC-to-POV metrics only; prose and NPC-to-NPC links are separate."""
    header = content.split("## К другим НПС", 1)[0]
    stats: dict[str, tuple[int, int]] = {}
    for line in header.splitlines():
        m = re.match(r"^\s*-\s*([^\n]+?)\s+(-?\d+)\s*/\s*([+-]?\d+)\s*$", line)
        if m:
            name = " ".join(m.group(1).strip().rstrip(":").casefold().split())
            value, change = map(int, m.groups()[1:])
            if name in stats:
                raise StateError(f"Повторяется показатель отношений: {path}: {name}")
            if not 0 <= value <= 100:
                raise StateError(f"Число отношения вне 0–100: {path}: {line.strip()}")
            if not -100 <= change <= 100:
                raise StateError(f"Изменение отношения вне -100..100: {path}: {line.strip()}")
            stats[name] = (value, change)
    if required and not stats:
        raise StateError(f"Нет числовых показателей NPC→POV: {path}; нужны 1–10 строк вида '- интерес 56/+0'")
    if len(stats) > 10:
        raise StateError(f"Больше 10 показателей отношений: {path}")
    if any(abs(change) > 3 for _, change in stats.values()):
        if "Причина резкого изменения:" not in content:
            raise StateError(f"Для скачка >3 нужна строка 'Причина резкого изменения:': {path}")
    return stats


def _validate_relation(content: str, path: str) -> None:
    if path.endswith("/relations.md"):
        _relation_stats(content, path, required=True)


def _validate_items(content: str) -> None:
    ids = re.findall(r"^##\s+(ITEM_[A-Za-z0-9_-]+)\b", content, re.MULTILINE)
    if len(ids) != len(set(ids)):
        raise StateError("В items.md повторяется ID предмета")


def _validate_all(changes: dict[str, str]) -> None:
    if not isinstance(changes, dict):
        raise StateError("files должен быть объектом {path: text}")
    for path, content in changes.items():
        _safe_change(path, content)
        _validate_relation(content, path)
        if path == "state/items.md":
            _validate_items(content)


def _validate_relation_updates(current: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        if not name.endswith("/relations.md"):
            continue
        old_file = current / name
        old = _relation_stats(old_file.read_text(encoding="utf-8"), name) if old_file.is_file() else {}
        new = _relation_stats(text, name, required=True)
        if set(old) - set(new):
            raise StateError(f"Нельзя удалять сохранённые показатели отношений: {name}")
        for label in old.keys() & new.keys():
            value, delta = new[label]
            if value != old[label][0] + delta:
                raise StateError(f"Значение и дельта не согласованы с прошлым ходом: {name}: {label}")


def _turn_payload(
    reader_text: str, narration: str, files: dict[str, str],
    timeline_events: list[str] | None, timeline_no_change_reason: str,
    packet_id: str | None,
) -> dict[str, Any]:
    """Canonical save payload; a receipt cannot authorize different text/state."""
    return {
        "reader_text": reader_text, "narration": narration, "files": files,
        "timeline_events": timeline_events,
        "timeline_no_change_reason": timeline_no_change_reason,
        "packet_id": packet_id,
    }


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _validate_turn_payload(payload: dict[str, Any]) -> list[tuple[str, str]]:
    if (not isinstance(payload["reader_text"], str)
            or not isinstance(payload["narration"], str)
            or not payload["narration"].strip()
            or len(payload["reader_text"]) > MAX_INPUT_FILE
            or len(payload["narration"]) > MAX_INPUT_FILE):
        raise StateError("Нужны полные сообщения читателя и писателя допустимого размера")
    files = payload["files"]
    if not isinstance(files, dict) or not isinstance(files.get("state/scene.md"), str) or not files["state/scene.md"].strip():
        raise StateError("Состояние сцены обязательно на каждом игровом ходу")
    if "state/timeline.md" in files:
        raise StateError("Хронология обновляется автоматически из timeline_events; не передавайте state/timeline.md в files")
    parsed = _parse_timeline_events(payload["timeline_events"])
    reason = payload["timeline_no_change_reason"]
    if not parsed and (not isinstance(reason, str) or len(reason.strip()) < 8):
        raise StateError("Укажите значимые timeline_events либо объясните, почему хронология за этот ход не изменилась")
    if parsed and reason:
        raise StateError("При наличии событий не нужен timeline_no_change_reason")
    _validate_all(files)
    return parsed



def _parse_timeline_events(events: list[str]) -> list[tuple[str, str]]:
    """Strict compact wire format: DD.MM.YYYY | a factual event."""
    if not isinstance(events, list) or len(events) > 30:
        raise StateError("timeline_events: требуется список из 0–30 событий")
    parsed: list[tuple[str, str]] = []
    for raw in events:
        if not isinstance(raw, str) or len(raw) > 550 or "\n" in raw:
            raise StateError("Каждое событие хронологии должно быть одной короткой строкой")
        match = TIMELINE_FORMAT.fullmatch(raw.strip())
        if match is None:
            raise StateError("Формат события: ДД.ММ.ГГГГ | краткий факт")
        date, event = match.groups()
        try:
            datetime.strptime(date, "%d.%m.%Y")
        except ValueError as exc:
            raise StateError("Неверная календарная дата события") from exc
        event = event.strip()
        if len(event) < 8 or event.startswith("["):
            raise StateError("Нужен конкретный краткий факт, а не пустой шаблон")
        parsed.append((date, event))
    if len(parsed) != len(set(parsed)):
        raise StateError("Одинаковое событие дважды в одном обновлении")
    return parsed


def _append_timeline(previous: str, events: list[tuple[str, str]]) -> str:
    """Preserve existing prose, append grouped dated facts, avoid exact duplicates."""
    if not events:
        return previous
    has_date = any(DATE_HEADING.fullmatch(line.strip())
                   for line in previous.splitlines())
    if "## [ДД.ММ.ГГГГ]" in previous and not has_date:
        # Drop the untouched original template ONLY if it contains no real
        # notes. Never erase user-authored events appended under the placeholder.
        template_lines = {
            TIMELINE_HEADER,
            "## [ДД.ММ.ГГГГ]",
            "- [Только событие, повлиявшее на сюжет, знания, отношения, положение дел или важные предметы.]",
            "- [Время добавлять, только если оно важно.]",
            "Факты: что произошло на самом деле, без выдуманных трактовок. Не записывать бытовые действия без последствий. Частные убеждения персонажей — только в их `knowledge.md`. Записи можно уплотнять без удаления фактов и причин.",
        }
        if all(not line.strip() or line.strip() in template_lines
               for line in previous.splitlines()):
            previous = TIMELINE_HEADER + "\n"
    lines = (previous.strip() or TIMELINE_HEADER).splitlines()
    for date, event in events:
        headings = [
            (i, found.group(1))
            for i, line in enumerate(lines)
            if (found := DATE_HEADING.fullmatch(line.strip()))
        ]
        existing = next(((index, number) for number, (index, label)
                         in enumerate(headings) if label == date), None)
        bullet = "- " + event
        if existing is not None:
            index, number = existing
            end = headings[number + 1][0] if number + 1 < len(headings) else len(lines)
            section = lines[index + 1:end]
            if bullet not in section:
                while end > index + 1 and not lines[end - 1].strip():
                    end -= 1
                lines.insert(end, bullet)
        else:
            later = next((i for i, label in headings
                          if datetime.strptime(label, "%d.%m.%Y") >
                             datetime.strptime(date, "%d.%m.%Y")), None)
            new_lines = ["", f"## {date}", bullet, ""]
            if later is None:
                lines.extend(new_lines)
            else:
                lines[later:later] = new_lines
    result = "\n".join(lines).rstrip() + "\n"
    if len(result) > MAX_INPUT_FILE:
        raise StateError("Хронология превышает допустимый размер файла")
    return result


class SessionStore:
    def __init__(
        self, data_root: str | Path, templates_root: str | Path,
        engine_root: str | Path | None = None,
        semantic_reviewer: Any | None = None,
    ):
        self.semantic_reviewer = semantic_reviewer or evaluate_semantic
        self.engine_root = (
            Path(engine_root).resolve()
            if engine_root is not None
            else Path(__file__).resolve().parents[1]
        )
        self.data_root = Path(data_root).resolve()
        self.sessions = self.data_root / "sessions"
        self.packets = self.data_root / "packets"
        self.templates = Path(templates_root).resolve()
        self.sessions.mkdir(parents=True, exist_ok=True)
        self.packets.mkdir(parents=True, exist_ok=True)

    def _root(self, session_id: str) -> Path:
        if not SESSION_ID.fullmatch(session_id):
            raise StateError("Неверный идентификатор сессии")
        path = self.sessions / session_id
        if not path.is_dir():
            raise StateError("Сессия не найдена")
        return path

    def _current(self, root: Path) -> dict[str, Any]:
        return json.loads((root / "current.json").read_text(encoding="utf-8"))

    def _revision(self, root: Path, version: int) -> Path:
        return root / "revisions" / f"v{version:08d}"

    @contextlib.contextmanager
    def _locked(self, root: Path) -> Iterator[None]:
        with (root / ".lock").open("a+b") as fp:
            fcntl.flock(fp, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fp, fcntl.LOCK_UN)

    def create_session(self, original: str, novel: str, pov: str) -> dict[str, Any]:
        for key, value in (("original", original), ("novel", novel), ("pov", pov)):
            if not isinstance(value, str) or not value.strip() or len(value) > MAX_INPUT_FILE:
                raise StateError(f"Нужен непустой корректный текст {key}")
        session_id = "CN_" + secrets.token_hex(16)
        root = self.sessions / session_id
        root.mkdir(mode=0o700)
        try:
            _atomic_write(root / "original.md", original)
            first = self._revision(root, 0)
            shutil.copytree(self.templates, first)
            (first / "original.md").unlink(missing_ok=True)
            (first / "system/session.json").unlink(missing_ok=True)
            _atomic_write(first / "novel.md", novel)
            _atomic_write(first / "pov.md", pov)
            (first / "system").mkdir(exist_ok=True)
            _atomic_write(first / "system/turns.jsonl", "")
            meta = {
                "session_id": session_id, "status": "setup", "version": 0,
                "turn": 0, "audit_15_due": False, "audit_60_due": False,
                "setup_contract": 2,
            }
            _atomic_write(root / "current.json", _json_dumps(meta))
            return meta
        except Exception:
            shutil.rmtree(root)
            raise

    def list_sessions(self) -> list[dict[str, Any]]:
        out = []
        for root in sorted(self.sessions.iterdir()):
            if root.is_dir() and SESSION_ID.fullmatch(root.name) and (root / "current.json").exists():
                out.append(self._current(root))
        return out

    def read_state(self, session_id: str) -> dict[str, Any]:
        return self._current(self._root(session_id))

    def read_original(self, session_id: str) -> str:
        return (self._root(session_id) / "original.md").read_text(encoding="utf-8")

    def _write_revision(
        self, root: Path, previous: dict[str, Any], files: dict[str, str],
        status: str | None = None, turn: int | None = None,
        journal_entry: dict[str, Any] | None = None,
        clear_audit: str | None = None,
    ) -> dict[str, Any]:
        _validate_all(files)
        current = self._revision(root, previous["version"])
        if journal_entry is not None:
            _validate_relation_updates(current, files)
        new_version = previous["version"] + 1
        final = self._revision(root, new_version)
        if final.exists():
            # Crash before pointer switch can leave an unreferenced revision.
            shutil.rmtree(final)
        stage = root / f".staging_{secrets.token_hex(8)}"
        try:
            shutil.copytree(current, stage, copy_function=os.link)
            for path, text in files.items():
                _atomic_write(stage / path, text)
            if journal_entry is not None:
                journal_path = stage / "system/turns.jsonl"
                journal = journal_path.read_text(encoding="utf-8")
                journal += json.dumps(journal_entry, ensure_ascii=False) + "\n"
                _atomic_write(journal_path, journal)
                turns = [json.loads(line) for line in journal.splitlines() if line.strip()]
                recent = turns[-3:]
                formatted = "# Последние три игровых ответа — дословно\n\n" + "\n\n".join(
                    f"## Ход {item['turn']}\n{item['narration']}" for item in recent
                )
                _atomic_write(stage / "state/recent.md", formatted + "\n")
            os.replace(stage, final)
            meta = dict(previous)
            meta["version"] = new_version
            if status is not None:
                meta["status"] = status
            if turn is not None:
                meta["turn"] = turn
                meta["audit_15_due"] = (turn > 0 and turn % 15 == 0)
                meta["audit_60_due"] = (turn > 0 and turn % 60 == 0)
            if clear_audit is not None:
                meta[f"audit_{clear_audit}_due"] = False
            _atomic_write(root / "current.json", _json_dumps(meta))
            return meta
        except Exception:
            if stage.exists():
                shutil.rmtree(stage)
            raise

    def update_setup(
        self, session_id: str, expected_version: int, files: dict[str, str],
    ) -> dict[str, Any]:
        root = self._root(session_id)
        with self._locked(root):
            prior = self._current(root)
            if prior["version"] != expected_version:
                raise StateError("Версия устарела; перечитайте состояние")
            if prior["turn"] != 0 or prior["status"] not in ("setup", "ready", "active"):
                raise StateError("Редактирование настройки запрещено после первого игрового хода")
            if prior["status"] != "setup":
                rev = self._revision(root, prior["version"])
                if (rev / "system/turns.jsonl").read_text(encoding="utf-8").strip():
                    raise StateError("В журнале уже есть игровой ход; настройка заблокирована")
                if not files:
                    raise StateError("Передайте исправленные файлы настройки")
            # A prelaunch correction reopens setup and invalidates old packets.
            # It preserves the same session/original and requires activation again.
            return self._write_revision(root, prior, files, status="setup")

    def setup_report(self, session_id: str) -> dict[str, Any]:
        """Find missing files and unresolved fields without inventing them."""
        root = self._root(session_id)
        meta = self._current(root)
        rev = self._revision(root, meta["version"])
        missing = []
        unresolved = []
        relation_errors = []
        npc_ids = []
        for name in ("novel.md", "pov.md", "state/scene.md", "state/locations.md"):
            p = rev / name
            if not p.is_file() or not p.read_text(encoding="utf-8").strip():
                missing.append(name)
        chars = rev / "characters"
        if chars.exists():
            for npc in sorted(chars.glob("NPC_*")):
                if not npc.is_dir():
                    continue
                npc_ids.append(npc.name)
                # The launch check must require every physical-participation
                # module, including abilities.md, just like prepare_turn.
                for name in CHAR_FILES:
                    p = npc / name
                    if not p.is_file() or not p.read_text(encoding="utf-8").strip():
                        missing.append(str(p.relative_to(rev)))
                relation = npc / "relations.md"
                if relation.is_file():
                    try:
                        _relation_stats(relation.read_text(encoding="utf-8"), str(relation.relative_to(rev)), required=True)
                    except StateError as exc:
                        relation_errors.append(str(exc))
        candidates = [
            rev / "novel.md", rev / "pov.md",
            *(rev / name for name in STATE_FILES if name != "state/recent.md"),
        ]
        if chars.exists():
            candidates += list(chars.glob("NPC_*/*.md"))
        # Do not silently approve story setups that still contain the sample
        # NPC roster/arcs. Empty timeline is valid until the first game turn.
        original_length = len((root / "original.md").read_text(encoding="utf-8"))
        npc_directories = list(chars.glob("NPC_*")) if chars.exists() else []
        if original_length > 6000 and not any(npc.is_dir() for npc in npc_directories):
            unresolved.append("characters/: длинный исходник, но нет ни одной анкеты NPC")
        for p in candidates:
            if not p.is_file():
                continue
            for line in p.read_text(encoding="utf-8").splitlines():
                if "не задано" in line.lower() or ("[" in line and "]" in line):
                    unresolved.append(f"{p.relative_to(rev)}: {line.strip()[:130]}")
        return {
            "session_id": session_id, "version": meta["version"],
            "missing_files": missing, "unresolved_fields": unresolved,
            "relation_errors": relation_errors,
            "npc_ids": npc_ids,
            "ready_without_approval": not missing and not unresolved and not relation_errors,
        }

    def activate(self, session_id: str, expected_version: int, reader_approved_gaps: bool = False) -> dict[str, Any]:
        root = self._root(session_id)
        with self._locked(root):
            prior = self._current(root)
            if prior["status"] != "setup" or prior["version"] != expected_version:
                raise StateError("Сессия уже запущена или версия устарела")
            report = self.setup_report(session_id)
            if report["missing_files"]:
                raise StateError(
                    "Заполните обязательные файлы до запуска: "
                    + ", ".join(report["missing_files"])
                )
            if report["relation_errors"]:
                raise StateError("Исправьте числовые отношения до запуска: " + "; ".join(report["relation_errors"]))
            if not report["ready_without_approval"] and not reader_approved_gaps:
                raise StateError("Сначала согласуйте пропуски с читателем; вызовите get_setup_report")
            return self._write_revision(root, prior, {}, status="ready")

    def _require_complete_turn_packet(
        self, session_id: str, version: int, packet_id: str | None,
    ) -> dict[str, Any]:
        """Called under the session lock, which also serializes read receipts."""
        if not isinstance(packet_id, str) or not re.fullmatch(r"[a-f0-9]{32}", packet_id):
            raise StateError(
                "Перед сохранением нужен packet_id от prepare_novella_turn и все его чанки"
            )
        path = self.packets / f"{packet_id}.json"
        if not path.is_file():
            raise StateError("Пакет контекста не найден; получите новый")
        packet = json.loads(path.read_text(encoding="utf-8"))
        if (packet.get("purpose") != "turn"
                or packet.get("session_id") != session_id
                or packet.get("version") != version):
            raise StateError("Пакет от другой сессии, другого хода или аудита")
        count = packet.get("count")
        if type(count) is not int or count < 1:
            raise StateError("Неверное число чанков")
        seen = packet.get("read_indices", [])
        if not isinstance(seen, list) or set(seen) != set(range(count)):
            missing = sorted(set(range(count)) - set(seen if isinstance(seen, list) else []))
            raise StateError(
                f"Контекст прочитан не полностью: не запрошены чанки {missing}"
            )
        return packet

    def _review_path(self, root: Path, version: int, reader_text: str) -> Path:
        # Fetching a new packet cannot reset the correction budget. A new
        # reader message starts a new cycle; do not invent one to bypass it.
        return root / "scene_reviews" / f"v{version:08d}" / f"{_digest(reader_text)}.json"

    def _plan_path(self, root: Path, version: int, reader_text: str) -> Path:
        return root / "scene_plans" / f"v{version:08d}" / f"{_digest(reader_text)}.json"

    def plan_scene(
        self, session_id: str, expected_version: int, reader_text: str,
        packet_id: str, plan: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist an explicit world/arc scan BEFORE scene review and publication."""
        if not isinstance(reader_text, str) or len(reader_text) > MAX_INPUT_FILE:
            raise StateError("Нужен дословный ввод читателя для плана сцены")
        required = {
            "basis", "active_arcs", "candidate_npcs", "time_span",
            "npc_initiative", "intended_shift", "possible_hook",
        }
        if not isinstance(plan, dict) or set(plan) != required:
            raise StateError("План: нужны basis, active_arcs, candidate_npcs, time_span, npc_initiative, intended_shift, possible_hook")
        for key in ("basis", "time_span", "npc_initiative", "intended_shift", "possible_hook"):
            value = plan[key]
            if not isinstance(value, str) or not 12 <= len(value.strip()) <= 750:
                raise StateError(f"План: {key} должен содержать конкретное объяснение (12–750 символов)")
        arcs, npcs = plan["active_arcs"], plan["candidate_npcs"]
        if (not isinstance(arcs, list) or len(arcs) > 40
                or any(not isinstance(x, str) or not re.fullmatch(r"(?:ARC|Q)_[A-Za-z0-9_-]+", x) for x in arcs)
                or len(arcs) != len(set(arcs))):
            raise StateError("План: active_arcs — список уникальных ID активных линий/вопросов")
        if (not isinstance(npcs, list) or len(npcs) > 100
                or any(not isinstance(x, str) or not re.fullmatch(r"NPC_[A-Za-z0-9_-]{1,60}", x) for x in npcs)
                or len(npcs) != len(set(npcs))):
            raise StateError("План: candidate_npcs — уникальные ID рассматриваемых NPC; пустой список допускается")
        root = self._root(session_id)
        with self._locked(root):
            meta = self._current(root)
            if (meta["version"] != expected_version or meta["status"] not in ("ready", "active")
                    or meta["audit_15_due"] or meta["audit_60_due"]):
                raise StateError("План: нужна актуальная активная сессия без ожидающих аудитов")
            packet = self._require_complete_turn_packet(session_id, expected_version, packet_id)
            if packet["participants"]:
                raise StateError("Сначала изучите мир с prepare_novella_turn(participants=[]), затем выберите действующих NPC")
            rev = self._revision(root, expected_version)
            arcs_content = (rev / "state/arcs.md").read_text(encoding="utf-8")
            for arc_id in arcs:
                if not re.search(r"(?m)^##\s+" + re.escape(arc_id) + r"\b", arcs_content):
                    raise StateError(f"План: нет существующей арки или вопроса {arc_id}")
            for npc_id in npcs:
                if not (rev / "characters" / npc_id / "core.md").is_file():
                    raise StateError(f"План: нет существующей анкеты {npc_id}")
            path = self._plan_path(root, expected_version, reader_text)
            record = {
                "version": expected_version, "reader_hash": _digest(reader_text),
                "source_packet": packet_id, "plan": plan,
            }
            if path.is_file():
                saved_plan = json.loads(path.read_text(encoding="utf-8"))
                if saved_plan.get("plan") != plan:
                    raise StateError("План для этого ввода уже зафиксирован; нельзя переписывать его задним числом")
                record = saved_plan  # Repeat reads/retries do not replace the first scan.
            else:
                _atomic_write(path, _json_dumps(record))
            return {"planned": True, "plan_id": _digest(record)[:32],
                    "candidate_npcs": npcs, "active_arcs": arcs}

    def _independent_scene_context(
        self, rev: Path, packet: dict[str, Any], plan: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Assemble original source documents server-side; the writer cannot forge them."""
        names = (
            "novel.md", "pov.md", "state/arcs.md", "state/roster.md",
            "state/scene.md", "state/locations.md", "state/items.md",
            "state/recent.md", "state/timeline.md",
        )
        state = {
            name: (rev / name).read_text(encoding="utf-8")
            for name in names if (rev / name).is_file()
        }
        npc_ids = {
            p["id"] for p in packet["participants"]
            if p["mode"] in ("physical", "phone", "observer", "message")
        } | set(plan["candidate_npcs"])
        characters = {}
        for npc_id in sorted(npc_ids):
            folder = rev / "characters" / npc_id
            if folder.is_dir():
                characters[npc_id] = {
                    name: (folder / name).read_text(encoding="utf-8")
                    for name in CHAR_FILES if (folder / name).is_file()
                }
        scout = {}
        for folder in sorted((rev / "characters").glob("NPC_*")):
            path = folder / "core.md"
            if path.is_file() and folder.name not in characters:
                scout[folder.name] = path.read_text(encoding="utf-8")[:1400]
        rules = {
            name: (self.engine_root / name).read_text(encoding="utf-8")
            for name in ENGINE_CONTEXT_FILES
        }
        journal = rev / "system/turns.jsonl"
        recent_turns = []
        if journal.is_file():
            for line in journal.read_text(encoding="utf-8").splitlines()[-3:]:
                item = json.loads(line)
                recent_turns.append({
                    "reader": item.get("reader", ""),
                    "narration": item.get("narration", ""),
                })
        context = {
            "rules": rules, "state": state, "characters": characters,
            "other_npc_index": scout, "recent_turns": recent_turns,
            "pre_scene_plan": plan, "participants": packet["participants"],
            "reader_input": payload["reader_text"],
            "draft_scene": payload["narration"],
            "proposed_changes": payload["files"],
            "timeline_events": payload["timeline_events"],
            "timeline_no_change_reason": payload["timeline_no_change_reason"],
        }
        if len(json.dumps(context, ensure_ascii=False)) > 420_000:
            raise ReviewUnavailable("Слишком большой контекст для независимой проверки; сократите объём карточек или истории")
        return context

    def review_scene(
        self, session_id: str, expected_version: int, reader_text: str,
        narration: str, files: dict[str, str], timeline_events: list[str],
        packet_id: str, checks: dict[str, Any],
        relationship_labels: dict[str, str], timeline_no_change_reason: str = "",
    ) -> dict[str, Any]:
        """Check one original draft plus at most two revised submissions."""
        if not isinstance(reader_text, str) or not isinstance(narration, str):
            raise StateError("Нужен дословный ввод читателя и полный текст сцены")
        if len(reader_text) > MAX_INPUT_FILE or len(narration) > MAX_INPUT_FILE:
            raise StateError("Превышен допустимый размер текста")
        payload = _turn_payload(reader_text, narration, files, timeline_events,
                                timeline_no_change_reason, packet_id)
        request_hash = _digest({"payload": payload, "checks": checks,
                                "relationship_labels": relationship_labels})
        root = self._root(session_id)
        with self._locked(root):
            meta = self._current(root)
            if meta["version"] != expected_version:
                raise StateError("Версия устарела; проверяйте сцену по новому состоянию")
            if meta["status"] not in ("ready", "active") or meta["audit_15_due"] or meta["audit_60_due"]:
                raise StateError("Сначала завершите настройку или обязательный аудит")
            packet = self._require_complete_turn_packet(session_id, expected_version, packet_id)
            plan_path = self._plan_path(root, expected_version, reader_text)
            if not plan_path.is_file():
                raise StateError("Перед review_novella_scene обязателен plan_novella_scene после изучения активного мира")
            if packet.get("review_contract") != 1:
                raise StateError("Получите новый prepare_novella_turn с engine/scene_review.md и прочитайте все чанки")
            path = self._review_path(root, expected_version, reader_text)
            ledger = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"attempts": []}
            attempts = ledger["attempts"]
            if attempts and attempts[-1]["request_hash"] == request_hash:
                return attempts[-1]["result"]  # Network retries consume no pass.
            # A failed version can be revised and reviewed again; no arbitrary
            # correction budget can force the user to accept a flawed scene.
            issues, footer = inspect_scene(narration, checks, relationship_labels)
            rev = self._revision(root, expected_version)
            try:
                _validate_turn_payload(payload)
                _validate_relation_updates(rev, files)
            except StateError as exc:
                issues.append(f"persistence: {exc}")
            expected_npcs = {p["id"] for p in packet["participants"]
                             if p["mode"] in ("physical", "phone", "observer")}
            if not isinstance(relationship_labels, dict) or set(relationship_labels) != expected_npcs:
                issues.append("skin.relationships: подписи должны включать всех участвующих NPC из пакета (physical/phone/observer), без упомянутых и посторонних")
            for npc_id in expected_npcs:
                name = f"characters/{npc_id}/relations.md"
                try:
                    if isinstance(files, dict) and name in files:
                        _safe_change(name, files[name])
                        expected = _relation_stats(files[name], name, required=True)
                    else:
                        before = _relation_stats((rev / name).read_text(encoding="utf-8"), name, required=True)
                        expected = {key: (value, 0) for key, (value, _) in before.items()}
                    if footer.get(npc_id) != expected:
                        issues.append(f"skin.relationships: {npc_id}: нижняя строка должна содержать все сохранённые шкалы с точным итогом и дельтой этого хода")
                except (StateError, OSError) as exc:
                    issues.append(f"skin.relationships: {npc_id}: {exc}")
            independent = None
            if not issues:
                plan = json.loads(plan_path.read_text(encoding="utf-8"))["plan"]
                context = self._independent_scene_context(rev, packet, plan, payload)
                # ReviewUnavailable is fail-closed: no approval, no saved turn,
                # and no revision attempt consumed during API outages.
                independent = self.semantic_reviewer(context)
                independent = validate_semantic_verdict(independent)
                for key in SEMANTIC_KEYS:
                    finding = independent["findings"][key]
                    if finding["status"] == "fail":
                        issues.append(
                            f"semantic.{key}: {finding['evidence']} "
                            f"Исправить: {finding['fix']}"
                        )
            correction = len(attempts)
            result = {
                "approved": not issues,
                "review_id": secrets.token_hex(16) if not issues else None,
                "correction_pass": correction,
                "corrections_remaining": None,
                "blocked": False,
                "issues": issues,
                "independent_review": independent,
            }
            attempts.append({
                "request_hash": request_hash, "payload_hash": _digest(payload),
                "checks": checks, "relationship_labels": relationship_labels,
                "result": result,
            })
            _atomic_write(path, _json_dumps(ledger))
            return result

    def _require_scene_review(
        self, root: Path, version: int, payload: dict[str, Any], review_id: str | None,
    ) -> dict[str, Any]:
        if not isinstance(review_id, str) or not re.fullmatch(r"[a-f0-9]{32}", review_id):
            raise StateError("Перед сохранением вызовите review_novella_scene; нужен одобренный review_id")
        path = self._review_path(root, version, payload["reader_text"])
        if not path.is_file():
            raise StateError("Нет проверки этого хода; вызовите review_novella_scene")
        review = json.loads(path.read_text(encoding="utf-8"))["attempts"][-1]
        if (not review["result"]["approved"] or review["result"]["review_id"] != review_id
                or review["payload_hash"] != _digest(payload)):
            raise StateError("Проверка не одобрила этот точный текст и изменения; не подменяйте сцену после проверки")
        if not review["result"].get("independent_review"):
            raise StateError("Нет обязательного независимого серверного заключения")
        return {"review_id": review_id, "correction_pass": review["result"]["correction_pass"],
                "checks": review["checks"], "relationship_labels": review["relationship_labels"],
                "independent_review": review["result"]["independent_review"]}

    def commit_turn(
        self, session_id: str, expected_version: int, reader_text: str,
        narration: str, files: dict[str, str],
        timeline_events: list[str] | None = None,
        timeline_no_change_reason: str = "",
        packet_id: str | None = None,
        review_id: str | None = None,
    ) -> dict[str, Any]:
        """Save only the exact reviewed scene/state after all current chunks."""
        payload = _turn_payload(reader_text, narration, files, timeline_events,
                                timeline_no_change_reason, packet_id)
        parsed = _validate_turn_payload(payload)
        root = self._root(session_id)
        with self._locked(root):
            prior = self._current(root)
            if prior["version"] != expected_version:
                raise StateError("Версия устарела; ход не сохранён")
            if prior["status"] not in ("ready", "active"):
                raise StateError("Сессия ещё не активирована")
            if prior["audit_15_due"] or prior["audit_60_due"]:
                raise StateError("Сначала завершите обязательный аудит")
            # It is not possible to prove the model UNDERSTOOD each chunk,
            # but the server can require all of them to have been fetched.
            self._require_complete_turn_packet(session_id, prior["version"], packet_id)
            review = self._require_scene_review(root, expected_version, payload, review_id)
            next_turn = prior["turn"] + 1
            changes = dict(files)
            if parsed:
                current = self._revision(root, prior["version"])
                before = (current / "state/timeline.md").read_text(encoding="utf-8")
                changes["state/timeline.md"] = _append_timeline(before, parsed)
            log_entry = {
                "turn": next_turn, "reader": reader_text, "narration": narration,
                "timeline_events": timeline_events,
                "timeline_no_change_reason": timeline_no_change_reason,
                "scene_review": review,
            }
            return self._write_revision(
                root, prior, changes, status="active", turn=next_turn,
                journal_entry=log_entry,
            )

    def initialize_relationship_metrics(
        self, session_id: str, expected_version: int,
        metrics: dict[str, dict[str, int]], basis: str,
    ) -> dict[str, Any]:
        """Append missing scales without rewriting prose, history or gameplay."""
        if not isinstance(metrics, dict) or not 1 <= len(metrics) <= 100:
            raise StateError("Нужны показатели для 1–100 существующих NPC")
        if not isinstance(basis, str) or not 8 <= len(basis.strip()) <= 2000:
            raise StateError("Укажите основание начальных оценок по канону")
        root = self._root(session_id)
        with self._locked(root):
            prior = self._current(root)
            if prior["version"] != expected_version:
                raise StateError("Версия устарела; показатели не записаны")
            if prior["status"] not in ("setup", "ready", "active"):
                raise StateError("Недопустимое состояние сессии")
            rev = self._revision(root, prior["version"])
            changes = {}
            sources = {}
            for npc, values in metrics.items():
                path = f"characters/{npc}/relations.md"
                if not isinstance(npc, str) or not re.fullmatch(r"NPC_[A-Za-z0-9_-]{1,60}", npc):
                    raise StateError("Неверный ID NPC")
                if not isinstance(values, dict) or not 1 <= len(values) <= 10:
                    raise StateError(f"Укажите 1–10 индивидуальных показателей: {npc}")
                normalized = {}
                for label, value in values.items():
                    if not isinstance(label, str) or not re.fullmatch(r"[^\n\r/#*]{1,60}", label.strip()):
                        raise StateError("Неверное название показателя")
                    key = " ".join(label.strip().rstrip(":").casefold().split())
                    if not key or key in normalized or type(value) is not int or not 0 <= value <= 100:
                        raise StateError(f"Неверный или повторяющийся показатель: {npc}")
                    normalized[key] = value
                target = rev / path
                if not target.is_file():
                    raise StateError(f"Сначала создайте анкету NPC: {path}")
                previous = target.read_text(encoding="utf-8")
                existing = _relation_stats(previous, path)
                if existing:
                    if {key: value for key, (value, _) in existing.items()} != normalized:
                        raise StateError(f"Показатели уже существуют; инициализация не может их менять: {npc}")
                    sources[npc] = {"kind": "already_present"}
                    continue
                historical = None
                for old in sorted((root / "revisions").glob("v[0-9]*"), reverse=True):
                    if old.name >= rev.name or not (old / path).is_file():
                        continue
                    try:
                        found = _relation_stats((old / path).read_text(encoding="utf-8"), path)
                    except StateError:
                        continue
                    if found:
                        historical = (old.name, {key: value for key, (value, _) in found.items()})
                        break
                if historical and historical[1] != normalized:
                    raise StateError(f"Найдены прежние значения {npc} в {historical[0]}; восстановите их вместо новых оценок: {historical[1]}")
                sources[npc] = {"kind": "restored" if historical else "initialized"}
                if historical:
                    sources[npc]["source_revision"] = historical[0]
                note = ("Восстановлены прежние шкалы" if historical else "Новые начальные оценки по канону")
                block = "## Показатели NPC→POV\n" + "\n".join(f"- {label} {value}/+0" for label, value in normalized.items())
                changes[path] = block + f"\n\n{note}; без игрового хода. Основание: " + " ".join(basis.splitlines()) + "\n\n" + previous
            if not changes:
                return {**prior, "metrics_sources": sources, "gameplay_turn_added": False, "already_present": True}
            updated = self._write_revision(root, prior, changes)
            return {**updated, "metrics_sources": sources, "gameplay_turn_added": False}

    def repair_timeline(
        self, session_id: str, expected_version: int,
        source_turns: list[int], timeline_events: list[str],
    ) -> dict[str, Any]:
        """Backfill confirmed facts from recorded turns without a gameplay turn."""
        parsed = _parse_timeline_events(timeline_events)
        if not parsed:
            raise StateError("Для восстановления нужны конкретные события")
        if not isinstance(source_turns, list) or not source_turns or any(
            type(value) is not int or value <= 0 for value in source_turns
        ):
            raise StateError("Укажите существующие номера исходных ходов")
        root = self._root(session_id)
        with self._locked(root):
            prior = self._current(root)
            if prior["version"] != expected_version:
                raise StateError("Версия устарела; ничего не восстановлено")
            if prior["status"] not in ("active", "ready"):
                raise StateError("Восстановление допустимо только для готовой новеллы")
            rev = self._revision(root, prior["version"])
            raw_journal = (rev / "system/turns.jsonl").read_text(encoding="utf-8")
            known_turns = {
                json.loads(line)["turn"] for line in raw_journal.splitlines()
                if line.strip()
            }
            if not set(source_turns).issubset(known_turns):
                raise StateError("Один из указанных исходных ходов отсутствует в журнале")
            old = (rev / "state/timeline.md").read_text(encoding="utf-8")
            new = _append_timeline(old, parsed)
            if new == old:
                return {
                    **prior, "repaired_from_turns": sorted(set(source_turns)),
                    "added_events": 0, "already_present": True,
                }
            updated = self._write_revision(
                root, prior, {"state/timeline.md": new}
            )
            return {
                **updated, "repaired_from_turns": sorted(set(source_turns)),
                "submitted_events": len(parsed), "gameplay_turn_added": False,
            }

    def commit_audit(
        self, session_id: str, expected_version: int, kind: int,
        files: dict[str, str],
    ) -> dict[str, Any]:
        if kind not in (15, 60):
            raise StateError("Допустимы только аудиты 15 и 60")
        if any(
            p not in ("state/timeline.md", "state/arcs.md", "state/roster.md", "state/items.md")
            and not p.endswith("/knowledge.md") for p in files
        ):
            raise StateError("Аудит меняет только файлы памяти/хронологии/состояния")
        root = self._root(session_id)
        with self._locked(root):
            prior = self._current(root)
            if prior["version"] != expected_version:
                raise StateError("Версия устарела")
            if not prior[f"audit_{kind}_due"]:
                raise StateError("Такой аудит сейчас не требуется")
            return self._write_revision(root, prior, files, clear_audit=str(kind))

    def _packet(
        self, session_id: str, version: int, parts: list[tuple[str, str]],
        purpose: str = "turn",
        participants: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        full = "".join(
            f"\n===== FILE {path} =====\n{text}\n===== END {path} =====\n"
            for path, text in parts
        )
        chunks = [
            full[i:i + CHUNK_SIZE]
            for i in range(0, len(full), CHUNK_SIZE)
        ] or [""]
        packet_id = secrets.token_hex(16)
        payload = {
            "session_id": session_id, "version": version, "chunks": chunks,
            "count": len(chunks), "total_characters": len(full),
            "purpose": purpose, "read_indices": [],
            "participants": participants or [],
            "review_contract": 1 if purpose == "turn" else None,
        }
        _atomic_write(self.packets / f"{packet_id}.json", _json_dumps(payload))
        return {
            "packet_id": packet_id, "version": version,
            "count": len(chunks), "total_characters": len(full),
            "chunk_unit": "unicode_characters", "chunk_limit": CHUNK_SIZE,
        }

    def prepare_turn(
        self, session_id: str, participants: list[dict[str, Any]],
    ) -> dict[str, Any]:
        root = self._root(session_id)
        meta = self._current(root)
        if meta["status"] not in ("ready", "active"):
            raise StateError("Завершите настройку сессии")
        if meta["audit_15_due"] or meta["audit_60_due"]:
            raise StateError("Требуется обязательный аудит до следующего хода")
        rev = self._revision(root, meta["version"])
        names = ["novel.md", "pov.md", *STATE_FILES]
        seen = set(names)
        modes = {
            "physical": CHAR_FILES,
            "phone": (
                "core.md", "voice.md", "personality.md",
                "knowledge.md", "relations.md",
            ),
            "observer": (
                "core.md", "personality.md", "abilities.md",
                "knowledge.md", "relations.md",
            ),
            "message": (),
            "mentioned": (),
        }
        if not isinstance(participants, list) or len(participants) > 100:
            raise StateError("Неверный список участников")
        if not participants:
            # Initial reconnaissance: all NPCs are considered before casting.
            # Short card excerpts are context for planning, not scene participants.
            index = []
            for card in sorted((rev / "characters").glob("NPC_*/core.md"))[:100]:
                npc = card.parent.name
                fragments = []
                for section in ("core.md", "personality.md", "relations.md"):
                    f = card.parent / section
                    if f.is_file():
                        fragments.append(section + ": " + f.read_text(encoding="utf-8")[:450])
                index.append(npc + "\n" + "\n".join(fragments))
            if index:
                names.append("system/npc_scout")  # added separately as generated packet text
        for info in participants:
            if not isinstance(info, dict):
                raise StateError("Участник должен иметь ID и режим")
            npc_id, mode = info.get("id"), info.get("mode")
            if not isinstance(npc_id, str) or not re.fullmatch(
                r"NPC_[A-Za-z0-9_-]{1,60}", npc_id
            ):
                raise StateError("Неверный ID НПС")
            if mode not in modes:
                raise StateError("Неизвестный режим участия НПС")
            chosen = list(modes[mode])
            if info.get("need_past") and mode in ("phone", "observer"):
                chosen.append("past.md")
            for filename in chosen:
                name = f"characters/{npc_id}/{filename}"
                if not (rev / name).is_file():
                    if meta.get("setup_contract", 1) >= 2:
                        raise StateError(
                            f"Для NPC {npc_id} нет обязательной анкеты {filename}. "
                            "Сначала заполните файлы персонажа при настройке"
                        )
                    # Older sessions may not have character files yet.
                    # Preserve their legacy behavior rather than breaking play.
                    continue
                if name not in seen:
                    names.append(name)
                    seen.add(name)
        # Каждый ход читаем все инструкции, включая проверку, из engine/.
        # Это не состояние новеллы, поэтому их нельзя редактировать через
        # save_setup_files/save_novella_turn и нет смысла копировать в /data.
        parts = []
        for path in ENGINE_CONTEXT_FILES:
            file = self.engine_root / path
            if not file.is_file():
                raise StateError(f"Отсутствует обязательная инструкция движка: {path}")
            parts.append((path, file.read_text(encoding="utf-8")))
        for path in names:
            file = rev / path
            if file.is_file():
                parts.append((path, file.read_text(encoding="utf-8")))
        if not participants and index:
            parts.append(("system/npc_scout", "\n\n".join(index)))
        packet = self._packet(session_id, meta["version"], parts, participants=participants)
        packet["engine_files"] = list(ENGINE_CONTEXT_FILES)
        packet["pre_save_review"] = (
            "Примените engine/scene_review.md к готовому черновику и вызовите "
            "review_novella_scene с восемью конкретными результатами. Сервер "
            "дополнительно вызывает независимого модельного рецензента. "
            "При ошибках исправляйте черновик и повторяйте проверку до одобрения. "
            "В save_novella_turn передайте одобренный review_id и тот же текст, "
            "файлы и хронологию. Затем опубликуйте сохранённый текст без отчёта."
        )
        packet["scene_review"] = {"tool": "review_novella_scene", "checks": list(REVIEW_CHECKS),
                                  "max_corrections": MAX_CORRECTIONS}
        return packet

    def prepare_audit(self, session_id: str, kind: int) -> dict[str, Any]:
        if kind not in (15, 60):
            raise StateError("Нужен аудит 15 или 60")
        root = self._root(session_id)
        meta = self._current(root)
        if not meta[f"audit_{kind}_due"]:
            raise StateError("Аудит пока не требуется")
        rev = self._revision(root, meta["version"])
        if kind == 15:
            journal = (rev / "system/turns.jsonl").read_text(encoding="utf-8")
            entries = [
                json.loads(line) for line in journal.splitlines() if line.strip()
            ][-15:]
            parts = [
                (
                    f"chat/turn_{entry['turn']}",
                    f"Читатель:\n{entry['reader']}\n\nGPT:\n{entry['narration']}",
                ) for entry in entries
            ]
            for name in (
                "state/timeline.md", "state/items.md",
                "state/roster.md", "state/arcs.md",
            ):
                parts.append(
                    (name, (rev / name).read_text(encoding="utf-8"))
                )
        else:
            parts = [
                ("state/timeline.md",
                 (rev / "state/timeline.md").read_text(encoding="utf-8")),
            ]
        for path in sorted((rev / "characters").glob("NPC_*/knowledge.md")):
            parts.append(
                (str(path.relative_to(rev)), path.read_text(encoding="utf-8"))
            )
        return self._packet(session_id, meta["version"], parts, purpose="audit")

    def read_chunk(self, packet_id: str, index: int) -> dict[str, Any]:
        """Deliver a chunk and persist a fetch receipt for this session version."""
        if not isinstance(packet_id, str) or not re.fullmatch(r"[a-f0-9]{32}", packet_id):
            raise StateError("Неверный ID пакета")
        packet_file = self.packets / f"{packet_id}.json"
        if not packet_file.is_file():
            raise StateError("Пакет не найден (или удалён при очистке)")
        initial = json.loads(packet_file.read_text(encoding="utf-8"))
        root = self._root(initial["session_id"])
        # Same session lock as commit_turn prevents racing with a save.
        with self._locked(root):
            packet = json.loads(packet_file.read_text(encoding="utf-8"))
            meta = self._current(root)
            if meta["version"] != packet["version"]:
                raise StateError("Пакет устарел: состояние изменилось, соберите заново")
            if type(index) is not int or not 0 <= index < packet["count"]:
                raise StateError("Неверный номер чанка")
            receipt = set(packet.get("read_indices", []))
            if index not in receipt:
                packet["read_indices"] = sorted(receipt | {index})
                _atomic_write(packet_file, _json_dumps(packet))
            return {
                "index": index, "count": packet["count"],
                "text": packet["chunks"][index], "version": packet["version"],
            }

