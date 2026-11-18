"""Gameplay must fetch all current context chunks; new setups must not be skeletons."""
import json
import tempfile
import unittest
from pathlib import Path

from chatnovella.store import CHUNK_SIZE, SessionStore, StateError

FILES = (
    "state/scene.md", "state/locations.md", "state/roster.md",
    "state/arcs.md", "state/timeline.md", "state/items.md", "state/recent.md",
)


class TestContextReceipt(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        template = base / "template"
        for name in FILES:
            file = template / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("Начальное состояние", encoding="utf-8")
        self.store = SessionStore(base / "data", template)
        meta = self.store.create_session("Оригинальный канон", "Канон", "POV")
        self.session_id = meta["session_id"]
        self.meta = self.store.activate(
            self.session_id, meta["version"], reader_approved_gaps=True
        )

    def tearDown(self):
        self.temp.cleanup()

    def commit(self, packet_id=None):
        return self.store.commit_turn(
            self.session_id, self.meta["version"], "Действие", "Новая сцена",
            {"state/scene.md": "Новая сцена"}, timeline_events=[],
            timeline_no_change_reason="Не произошло ничего значимого",
            packet_id=packet_id,
        )

    def test_missing_packet_and_partial_packet_both_block(self):
        with self.assertRaisesRegex(StateError, "packet_id"):
            self.commit()
        edit = self.store._write_revision(
            self.store._root(self.session_id), self.meta,
            {"state/timeline.md": "Я" * (CHUNK_SIZE * 2)}
        )
        self.meta = edit
        packet = self.store.prepare_turn(self.session_id, [])
        self.assertGreaterEqual(packet["count"], 3)
        self.store.read_chunk(packet["packet_id"], 0)
        with self.assertRaisesRegex(StateError, "не запрошены чанки"):
            self.commit(packet["packet_id"])
        self.assertEqual(self.store.read_state(self.session_id)["turn"], 0)
        for index in reversed(range(1, packet["count"])):
            self.store.read_chunk(packet["packet_id"], index)
        saved = self.commit(packet["packet_id"])
        self.assertEqual(saved["turn"], 1)
        self.assertGreater(saved["version"], self.meta["version"])

    def test_wrong_session_and_audit_packet_rejected(self):
        other_meta = self.store.create_session("Другой исходник", "Канон 2", "POV 2")
        other = self.store.activate(
            other_meta["session_id"], other_meta["version"],
            reader_approved_gaps=True
        )
        foreign = self.store.prepare_turn(other_meta["session_id"], [])
        for i in range(foreign["count"]):
            self.store.read_chunk(foreign["packet_id"], i)
        with self.assertRaisesRegex(StateError, "другой сессии"):
            self.commit(foreign["packet_id"])
        audit = self.store._packet(
            self.session_id, self.meta["version"],
            [("audit", "Нельзя использовать для игрового хода")],
            purpose="audit",
        )
        self.store.read_chunk(audit["packet_id"], 0)
        with self.assertRaisesRegex(StateError, "аудита"):
            self.commit(audit["packet_id"])
        self.assertEqual(self.store.read_state(self.session_id)["turn"], 0)

    def test_old_packet_invalid_after_a_saved_turn(self):
        packet = self.store.prepare_turn(self.session_id, [])
        for i in range(packet["count"]):
            self.store.read_chunk(packet["packet_id"], i)
        self.commit(packet["packet_id"])
        with self.assertRaisesRegex(StateError, "устарел"):
            self.store.read_chunk(packet["packet_id"], 0)

    def test_setup_report_flags_roster_arcs_and_missing_npc_profiles(self):
        bad = self.store.create_session("Сюжет длинный " * 700, "Лор", "Героиня")
        edit = self.store.update_setup(bad["session_id"], bad["version"], {
            "state/roster.md": "- NPC_001 — [имя, роль]",
            "state/arcs.md": "## ARC_001 — [имя НПС]",
        })
        report = self.store.setup_report(bad["session_id"])
        self.assertTrue(any("state/roster.md" in item for item in report["unresolved_fields"]))
        self.assertTrue(any("state/arcs.md" in item for item in report["unresolved_fields"]))
        self.assertTrue(any("нет ни одной анкеты NPC" in item for item in report["unresolved_fields"]))
        with self.assertRaises(StateError):
            self.store.activate(bad["session_id"], edit["version"])

    def test_new_sessions_reject_missing_participant_cards_legacy_unaffected(self):
        with self.assertRaisesRegex(StateError, "обязательной анкеты"):
            self.store.prepare_turn(
                self.session_id, [{"id": "NPC_001", "mode": "physical"}]
            )
        root = self.store._root(self.session_id)
        old = self.store._current(root)
        old.pop("setup_contract")
        (root / "current.json").write_text(
            json.dumps(old, ensure_ascii=False), encoding="utf-8"
        )
        packet = self.store.prepare_turn(
            self.session_id, [{"id": "NPC_001", "mode": "physical"}]
        )
        self.assertGreater(packet["count"], 0)


if __name__ == "__main__":
    unittest.main()
