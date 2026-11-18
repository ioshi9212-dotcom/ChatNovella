"""Recover a prematurely activated setup without losing the original or a turn."""
import json
import tempfile
import unittest

from review_helpers import reviewed_commit
from pathlib import Path

from chatnovella.store import CHAR_FILES, STATE_FILES, SessionStore, StateError


class TestPrelaunchSetup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        template = base / "templates"
        for name in STATE_FILES:
            target = template / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("Начальное состояние", encoding="utf-8")
        self.store = SessionStore(base / "data", template)
        self.original = "Дословный оригинал\nВторая строка!\n"
        self.meta = self.store.create_session(self.original, "Канон", "POV")
        self.sid = self.meta["session_id"]
        self.ability = "characters/NPC_001/abilities.md"

    def profiles(self, abilities=True):
        return {
            f"characters/NPC_001/{name}": ("- интерес 50/+0" if name == "relations.md"
                                          else f"Подтверждённый канон: {name}")
            for name in CHAR_FILES if abilities or name != "abilities.md"
        }

    def legacy_ready(self):
        self.meta = self.store.update_setup(
            self.sid, self.meta["version"], self.profiles(abilities=False)
        )
        # Reproduce data accepted by the old activation check.
        self.meta = self.store._write_revision(
            self.store._root(self.sid), self.meta, {}, status="ready"
        )
        return self.meta

    def file(self, name, meta=None):
        meta = meta or self.store.read_state(self.sid)
        return self.store._revision(
            self.store._root(self.sid), meta["version"]
        ) / name

    def test_missing_abilities_reported_and_cannot_be_approved_away(self):
        self.meta = self.store.update_setup(
            self.sid, self.meta["version"], self.profiles(abilities=False)
        )
        self.assertIn(self.ability, self.store.setup_report(self.sid)["missing_files"])
        for approved in (False, True):
            with self.assertRaisesRegex(StateError, "abilities.md"):
                self.store.activate(self.sid, self.meta["version"], approved)
        self.assertEqual(self.store.read_state(self.sid), self.meta)

    def test_empty_required_module_also_blocks_activation(self):
        files = self.profiles()
        files[self.ability] = " \n\t"
        self.meta = self.store.update_setup(self.sid, self.meta["version"], files)
        self.assertIn(self.ability, self.store.setup_report(self.sid)["missing_files"])
        with self.assertRaisesRegex(StateError, "abilities.md"):
            self.store.activate(self.sid, self.meta["version"], True)

    def test_repair_same_unplayed_session_then_recheck_and_launch(self):
        before = self.legacy_ready()
        original_bytes = (self.store._root(self.sid) / "original.md").read_bytes()
        with self.assertRaisesRegex(StateError, "abilities.md"):
            self.store.prepare_turn(self.sid, [{"id": "NPC_001", "mode": "physical"}])
        old_packet = self.store.prepare_turn(self.sid, [])
        fixed = self.store.update_setup(self.sid, before["version"], {
            self.ability: "Способностей сверх обычных человеческих нет.",
        })
        self.assertEqual(fixed["session_id"], self.sid)
        self.assertEqual(fixed["status"], "setup")
        self.assertEqual(fixed["turn"], 0)
        self.assertEqual(fixed["version"], before["version"] + 1)
        self.assertEqual((self.store._root(self.sid) / "original.md").read_bytes(), original_bytes)
        self.assertEqual(self.file("system/turns.jsonl").read_text(), "")
        self.assertFalse(self.file(self.ability, before).exists())
        self.assertEqual(self.file("characters/NPC_001/core.md").read_text(),
                         self.file("characters/NPC_001/core.md", before).read_text())
        with self.assertRaisesRegex(StateError, "настройку"):
            self.store.prepare_turn(self.sid, [])
        with self.assertRaisesRegex(StateError, "устарел"):
            self.store.read_chunk(old_packet["packet_id"], 0)
        self.assertTrue(self.store.setup_report(self.sid)["ready_without_approval"])
        ready = self.store.activate(self.sid, fixed["version"])
        self.assertEqual(ready["turn"], 0)
        packet = self.store.prepare_turn(self.sid, [{"id": "NPC_001", "mode": "physical"}])
        text = "".join(self.store.read_chunk(packet["packet_id"], i)["text"]
                       for i in range(packet["count"]))
        self.assertIn(f"===== FILE {self.ability} =====", text)
        self.assertEqual(len(self.store.list_sessions()), 1)

    def test_stale_version_and_original_overwrite_cannot_reopen_setup(self):
        before = self.legacy_ready()
        with self.assertRaisesRegex(StateError, "Версия устарела"):
            self.store.update_setup(self.sid, before["version"] - 1, {self.ability: "Нет"})
        with self.assertRaisesRegex(StateError, "Недопустимый путь"):
            self.store.update_setup(self.sid, before["version"], {"original.md": "Подмена"})
        self.assertEqual(self.store.read_state(self.sid), before)
        self.assertEqual(self.store.read_original(self.sid), self.original)

    def test_first_saved_turn_keeps_setup_locked(self):
        edit = self.store.update_setup(self.sid, self.meta["version"], self.profiles())
        ready = self.store.activate(self.sid, edit["version"])
        packet = self.store.prepare_turn(self.sid, [{"id": "NPC_001", "mode": "physical"}])
        for i in range(packet["count"]):
            self.store.read_chunk(packet["packet_id"], i)
        played = reviewed_commit(self.store,
            self.sid, ready["version"], "Запускай первую сцену", "Первая сцена",
            {"state/scene.md": "Сцена началась"}, timeline_events=[],
            timeline_no_change_reason="Вступление без значимых событий",
            packet_id=packet["packet_id"],
            relationship_labels={"NPC_001": "Персонаж"},
            relationships="Персонаж — интерес 50/0",
        )
        with self.assertRaisesRegex(StateError, "после первого"):
            self.store.update_setup(self.sid, played["version"], {self.ability: "Подмена"})
        self.assertEqual(self.store.read_state(self.sid), played)
        self.assertIn("Первая сцена", self.file("system/turns.jsonl").read_text())

    def test_nonempty_journal_blocks_recovery_even_if_counter_is_zero(self):
        before = self.legacy_ready()
        self.file("system/turns.jsonl").write_text(
            json.dumps({"turn": 1, "reader": "Читатель", "narration": "Сцена"}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(StateError, "В журнале"):
            self.store.update_setup(self.sid, before["version"], {self.ability: "Нет"})
        self.assertEqual(self.store.read_state(self.sid), before)


if __name__ == "__main__":
    unittest.main()
