"""Each gameplay packet includes complete, current engine instructions."""
import tempfile
import unittest
from pathlib import Path

from chatnovella.store import (
    CHUNK_SIZE, ENGINE_CONTEXT_FILES, SessionStore, StateError,
)

STATE_FILES = (
    "state/scene.md", "state/locations.md", "state/roster.md",
    "state/arcs.md", "state/timeline.md", "state/items.md", "state/recent.md",
)


class TestEngineContext(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.engine = self.base / "app"
        self.templates = self.base / "templates"
        for index, name in enumerate(ENGINE_CONTEXT_FILES):
            target = self.engine / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"ENGINE_MARKER_{index}\n", encoding="utf-8")
        for name in STATE_FILES:
            target = self.templates / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"TEMPLATE_{name}\n", encoding="utf-8")
        self.store = SessionStore(
            self.base / "data", self.templates, engine_root=self.engine
        )
        metadata = self.store.create_session("RAW_ORIGINAL", "NOVEL_CANON", "POV_CARD")
        self.sid = metadata["session_id"]
        self.ready = self.store.activate(
            self.sid, metadata["version"], reader_approved_gaps=True
        )

    def tearDown(self):
        self.temp.cleanup()

    def unpack(self, packet):
        chunks = [self.store.read_chunk(packet["packet_id"], index)["text"]
                  for index in range(packet["count"])]
        self.assertTrue(all(len(chunk) <= CHUNK_SIZE for chunk in chunks))
        return "".join(chunks)

    def test_all_rules_and_skin_before_canon_every_scene(self):
        packet = self.store.prepare_turn(self.sid, [])
        self.assertEqual(packet["engine_files"], list(ENGINE_CONTEXT_FILES))
        self.assertEqual(len(ENGINE_CONTEXT_FILES), 7)
        self.assertTrue(all(name.startswith("engine/") for name in ENGINE_CONTEXT_FILES))
        full = self.unpack(packet)
        self.assertNotIn("RAW_ORIGINAL", full)
        locations = [
            full.index(f"===== FILE {name} =====") for name in ENGINE_CONTEXT_FILES
        ]
        self.assertEqual(locations, sorted(locations))
        self.assertLess(locations[-1], full.index("===== FILE novel.md ====="))
        for index in range(len(ENGINE_CONTEXT_FILES)):
            self.assertIn(f"ENGINE_MARKER_{index}", full)

        revision = self.store._revision(
            self.store._root(self.sid), self.ready["version"]
        )
        for name in ENGINE_CONTEXT_FILES:
            self.assertFalse((revision / name).exists())

    def test_engine_read_fresh_every_turn_no_session_copy(self):
        first = self.unpack(self.store.prepare_turn(self.sid, []))
        path = self.engine / "engine/skin_builder.md"
        path.write_text("NEW_SKIN_VERSION_2", encoding="utf-8")
        second_packet = self.store.prepare_turn(self.sid, [])
        second = self.unpack(second_packet)
        self.assertNotIn("NEW_SKIN_VERSION_2", first)
        self.assertIn("NEW_SKIN_VERSION_2", second)
        meta = self.store.commit_turn(
            self.sid, self.ready["version"], "Ход читателя",
            "Ответ GPT", {"state/scene.md": "Новая поза"},
            timeline_events=[], timeline_no_change_reason="Это лишь смена позы",
            packet_id=second_packet["packet_id"],
        )
        third = self.unpack(self.store.prepare_turn(self.sid, []))
        self.assertIn("NEW_SKIN_VERSION_2", third)
        self.assertIn("Новая поза", third)
        self.assertEqual(meta["turn"], 1)

    def test_no_edit_engine_rules_via_session_state(self):
        with self.assertRaises(StateError):
            self.store.commit_turn(
                self.sid, self.ready["version"], "Действие", "Сцена",
                {"state/scene.md": "Сидит", "engine/skin_builder.md": "Подмена"},
                timeline_events=[], timeline_no_change_reason="Обычный ход без событий"
            )

    def test_required_rule_absent_fails_closed(self):
        (self.engine / ENGINE_CONTEXT_FILES[0]).unlink()
        with self.assertRaisesRegex(StateError, "обязательная инструкция"):
            self.store.prepare_turn(self.sid, [])

    def test_large_engine_module_transmitted_without_loss(self):
        huge = "У" * (CHUNK_SIZE * 2 + 15)
        path = self.engine / "engine/director.md"
        path.write_text(huge, encoding="utf-8")
        packet = self.store.prepare_turn(self.sid, [])
        self.assertGreaterEqual(packet["count"], 3)
        full = self.unpack(packet)
        self.assertIn(huge, full)
        self.assertEqual(packet["total_characters"], len(full))


if __name__ == "__main__":
    unittest.main()
