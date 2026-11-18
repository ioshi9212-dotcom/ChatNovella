"""Offline tests for ChatNovella state storage, with no Amvera account required."""
import tempfile
import unittest

from review_helpers import reviewed_commit
from pathlib import Path

from chatnovella.store import CHUNK_SIZE, SessionStore, StateError

FILES = [
    "state/scene.md", "state/locations.md", "state/roster.md",
    "state/arcs.md", "state/timeline.md", "state/items.md",
    "state/recent.md",
]


class TestStore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        template = base / "templates"
        template.mkdir()
        for name in FILES:
            file = template / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(f"Шаблон {name}", encoding="utf-8")
        self.store = SessionStore(base / "data", template)
        self.meta = self.store.create_session("Исходник 1:1\n", "Канон\n", "POV\n")
        self.id = self.meta["session_id"]

    def tearDown(self):
        self.temp.cleanup()

    def ready(self):
        meta = self.store.update_setup(self.id, 0, {
            "characters/NPC_001/core.md": "Сайлас",
            "characters/NPC_001/appearance.md": "Высокий",
            "characters/NPC_001/voice.md": "Низкий голос",
            "characters/NPC_001/personality.md": "Сдержанный",
            "characters/NPC_001/abilities.md": "Отражения",
            "characters/NPC_001/past.md": "Прошлое",
            "characters/NPC_001/knowledge.md": "Приватный секрет",
            "characters/NPC_001/relations.md": "- интерес 56/+2",
            "state/scene.md": "Сидит рядом",
        })
        return self.store.activate(self.id, meta["version"], reader_approved_gaps=True)

    def packet_text(self, packet):
        return "".join(
            self.store.read_chunk(packet["packet_id"], index)["text"]
            for index in range(packet["count"])
        )

    def complete_packet(self):
        packet = self.store.prepare_turn(self.id, [])
        self.packet_text(packet)
        return packet["packet_id"]

    def test_setup_report_requires_reader_approval(self):
        edit = self.store.update_setup(
            self.id, 0, {"pov.md": "Имя: не задано"}
        )
        report = self.store.setup_report(self.id)
        self.assertFalse(report["ready_without_approval"])
        self.assertTrue(report["unresolved_fields"])
        with self.assertRaises(StateError):
            self.store.activate(self.id, edit["version"])
        self.assertEqual(self.store.read_state(self.id)["status"], "setup")
        approved = self.store.activate(
            self.id, edit["version"], reader_approved_gaps=True
        )
        self.assertEqual(approved["status"], "ready")

    def test_original_immutable_and_independent_sessions(self):
        self.assertEqual(self.store.read_original(self.id), "Исходник 1:1\n")
        other = self.store.create_session("Другой", "Канон", "POV")
        self.assertNotEqual(self.id, other["session_id"])
        with self.assertRaises(StateError):
            self.store.update_setup(self.id, 0, {"original.md": "подмена"})
        with self.assertRaises(StateError):
            self.store.update_setup(self.id, 0, {"../secret.md": "подмена"})

    def test_phone_and_observer_selective_knowledge(self):
        self.ready()
        phone = self.packet_text(self.store.prepare_turn(
            self.id, [{"id": "NPC_001", "mode": "phone"}]
        ))
        self.assertIn("Низкий голос", phone)
        self.assertIn("Приватный секрет", phone)
        self.assertNotIn("Высокий", phone)
        self.assertNotIn("Прошлое", phone)
        self.assertNotIn("Исходник 1:1", phone)
        observer = self.packet_text(self.store.prepare_turn(
            self.id, [{"id": "NPC_001", "mode": "observer"}]
        ))
        self.assertIn("Отражения", observer)
        self.assertNotIn("Низкий голос", observer)

    def test_mentioned_or_message_no_character_card(self):
        self.ready()
        for mode in ("mentioned", "message"):
            text = self.packet_text(self.store.prepare_turn(
                self.id, [{"id": "NPC_001", "mode": mode}]
            ))
            self.assertNotIn("Приватный секрет", text)

    def test_large_chunks_complete_and_stale_packet_fails(self):
        meta = self.store.update_setup(self.id, 0, {
            "state/timeline.md": "Я" * (CHUNK_SIZE * 2 + 15),
        })
        self.store.activate(self.id, meta["version"], reader_approved_gaps=True)
        packet = self.store.prepare_turn(self.id, [])
        self.assertGreaterEqual(packet["count"], 3)
        chunks = [
            self.store.read_chunk(packet["packet_id"], i)["text"]
            for i in range(packet["count"])
        ]
        self.assertTrue(all(len(c) <= CHUNK_SIZE for c in chunks))
        self.assertIn("Я" * (CHUNK_SIZE * 2 + 15), "".join(chunks))
        reviewed_commit(self.store,
            self.id, self.store.read_state(self.id)["version"],
            "Действие", "Текст", {"state/scene.md": "новая поза"},
            timeline_events=[], timeline_no_change_reason="Значимого события нет",
            packet_id=packet["packet_id"],
        )
        with self.assertRaises(StateError):
            self.store.read_chunk(packet["packet_id"], 0)

    def test_commit_previous_state_protected_and_recent(self):
        ready = self.ready()
        meta = reviewed_commit(self.store,
            self.id, ready["version"], "Забираю зажигалку",
            "Лиам отдал зажигалку", {
                "state/scene.md": "Зажигалка у POV",
                "state/items.md": (
                    "## ITEM_001 — зажигалка\nУ кого физически: POV\n"
                ),
            },
            timeline_events=["02.05.2024 | Лиам передал Ринате зажигалку."],
            packet_id=self.complete_packet(),
        )
        self.assertEqual(meta["turn"], 1)
        with self.assertRaises(StateError):
            reviewed_commit(self.store,
                self.id, ready["version"], "1", "2",
                {"state/scene.md": "устаревшая версия"},
                timeline_events=[], timeline_no_change_reason="Новых фактов нет",
                packet_id=self.complete_packet(),
            )
        blob = self.packet_text(self.store.prepare_turn(self.id, []))
        self.assertIn("Лиам отдал зажигалку", blob)
        self.assertIn("У кого физически: POV", blob)
        self.assertNotIn("Исходник 1:1", blob)

    def test_bad_numbers_or_duplicated_item_rejected(self):
        with self.assertRaises(StateError):
            self.store.update_setup(self.id, 0, {
                "characters/NPC_001/relations.md": "- доверие 120/+2"
            })
        with self.assertRaises(StateError):
            self.store.update_setup(self.id, 0, {
                "characters/NPC_001/relations.md": "- доверие 10/-40"
            })
        with self.assertRaises(StateError):
            self.store.update_setup(self.id, 0, {
                "state/items.md": "## ITEM_001 — A\n## ITEM_001 — B"
            })

    def test_15_60_audits_block_next_turn_until_done(self):
        meta = self.ready()
        for number in range(1, 61):
            meta = reviewed_commit(self.store,
                self.id, meta["version"], f"reader-{number}",
                f"writer-{number}", {"state/scene.md": f"Сцена {number}"},
                timeline_events=[], timeline_no_change_reason="Никаких значимых изменений",
                packet_id=self.complete_packet(),
            )
            if number % 15 == 0:
                self.assertTrue(meta["audit_15_due"])
                audit = self.packet_text(self.store.prepare_audit(self.id, 15))
                self.assertIn(f"reader-{number}", audit)
                with self.assertRaises(StateError):
                    self.store.prepare_turn(self.id, [])
                meta = self.store.commit_audit(self.id, meta["version"], 15, {})
            if number % 60 == 0:
                self.assertTrue(meta["audit_60_due"])
                audit = self.packet_text(self.store.prepare_audit(self.id, 60))
                self.assertIn("state/timeline.md", audit)
                self.assertNotIn("reader-60", audit)
                meta = self.store.commit_audit(self.id, meta["version"], 60, {})
        self.assertEqual(meta["turn"], 60)
        self.assertFalse(meta["audit_15_due"])
        self.assertFalse(meta["audit_60_due"])


if __name__ == "__main__":
    unittest.main()
