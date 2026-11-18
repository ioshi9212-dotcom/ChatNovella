"""Persistent versioned ChatNovella sessions.

Narrative state lives as readable Markdown. A revision contains a complete
immutable snapshot; current.json changes atomically at successful commit.
"""
from __future__ import annotations

import contextlib
import fcntl
from datetime import datetime
import json
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterator

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


def _validate_relation(content: str, path: str) -> None:
    if not path.endswith("/relations.md"):
        return
    header = content.split("## К другим НПС", 1)[0]
    stats = []
    for line in header.splitlines():
        m = re.match(r"^\s*-\s*[^\n]+?\s+(-?\d+)\s*/\s*([+-]?\d+)\s*$", line)
        if m:
            value, change = map(int, m.groups())
            if not 0 <= value <= 100:
                raise StateError(f"Число отношения вне 0–100: {path}: {line.strip()}")
            if not -100 <= change <= 100:
                raise StateError(f"Изменение отношения вне -100..100: {path}: {line.strip()}")
            stats.append((value, change))
    if len(stats) > 10:
        raise StateError(f"Больше 10 показателей отношений: {path}")
    if any(abs(change) > 3 for _, change in stats):
        if "Причина резкого изменения:" not in content:
            raise StateError(f"Для скачка >3 нужна строка 'Причина резкого изменения:': {path}")


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
    ):
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
        for name in ("novel.md", "pov.md", "state/scene.md", "state/locations.md"):
            p = rev / name
            if not p.is_file() or not p.read_text(encoding="utf-8").strip():
                missing.append(name)
        chars = rev / "characters"
        if chars.exists():
            for npc in sorted(chars.glob("NPC_*")):
                if not npc.is_dir():
                    continue
                # The launch check must require every physical-participation
                # module, including abilities.md, just like prepare_turn.
                for name in CHAR_FILES:
                    p = npc / name
                    if not p.is_file() or not p.read_text(encoding="utf-8").strip():
                        missing.append(str(p.relative_to(rev)))
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
            "ready_without_approval": not missing and not unresolved,
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
            if not report["ready_without_approval"] and not reader_approved_gaps:
                raise StateError("Сначала согласуйте пропуски с читателем; вызовите get_setup_report")
            return self._write_revision(root, prior, {}, status="ready")

    def _require_complete_turn_packet(
        self, session_id: str, version: int, packet_id: str | None,
    ) -> None:
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

    def commit_turn(
        self, session_id: str, expected_version: int, reader_text: str,
        narration: str, files: dict[str, str],
        timeline_events: list[str] | None = None,
        timeline_no_change_reason: str = "",
        packet_id: str | None = None,
    ) -> dict[str, Any]:
        """Every turn requires explicit timeline review and fully fetched context."""
        if not isinstance(reader_text, str) or not isinstance(narration, str) or not narration.strip():
            raise StateError("Нужны полные сообщения читателя и писателя")
        if not isinstance(files, dict) or "state/scene.md" not in files:
            raise StateError("Состояние сцены обязательно на каждом игровом ходу")
        if "state/timeline.md" in files:
            raise StateError(
                "Хронология обновляется автоматически из timeline_events; "
                "не передавайте state/timeline.md в files"
            )
        # No implicit fallback: a forgotten chronology check is an error.
        parsed = _parse_timeline_events(timeline_events)
        if not parsed:
            if not isinstance(timeline_no_change_reason, str) or len(
                timeline_no_change_reason.strip()
            ) < 8:
                raise StateError(
                    "Укажите значимые timeline_events либо объясните, "
                    "почему хронология за этот ход не изменилась"
                )
        elif timeline_no_change_reason:
            raise StateError("При наличии событий не нужен timeline_no_change_reason")
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
            }
            return self._write_revision(
                root, prior, changes, status="active", turn=next_turn,
                journal_entry=log_entry,
            )

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
        # Каждый ход читаем 7 общих правил из одной папки engine/ на Amvera.
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
        packet = self._packet(session_id, meta["version"], parts)
        packet["engine_files"] = list(ENGINE_CONTEXT_FILES)
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

