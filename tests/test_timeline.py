"""Chronology must never be silently skipped; backfill preserves saved gameplay."""
import tempfile
import unittest

from review_helpers import reviewed_commit
from pathlib import Path

from chatnovella.store import SessionStore, StateError, _append_timeline

STATE = (
    "state/scene.md", "state/locations.md", "state/roster.md",
    "state/arcs.md", "state/timeline.md", "state/items.md",
    "state/recent.md",
)


class TestTimelineIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        templates = root / "templates"
        for filename in STATE:
            p = templates / filename
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# Хронология — строго приватно\n" if filename.endswith(
                "timeline.md"
            ) else filename, encoding="utf-8")
        self.store = SessionStore(root / "data", templates)
        meta = self.store.create_session("Оригинал", "История", "POV")
        self.sid = meta["session_id"]
        self.meta = self.store.activate(
            self.sid, meta["version"], reader_approved_gaps=True
        )

    def tearDown(self):
        self.tmp.cleanup()

    def files(self, name):
        root = self.store._root(self.sid)
        meta = self.store.read_state(self.sid)
        return (self.store._revision(root, meta["version"]) / name).read_text(
            encoding="utf-8"
        )

    def complete_packet(self):
        packet = self.store.prepare_turn(self.sid, [])
        for index in range(packet["count"]):
            self.store.read_chunk(packet["packet_id"], index)
        return packet["packet_id"]

    def save(self, scene="Сцена", events=None, reason="Это обычная реплика без сюжетного события"):
        self.meta = reviewed_commit(self.store,
            self.sid, self.meta["version"], "Реплика читателя", scene,
            {"state/scene.md": scene},
            timeline_events=events, timeline_no_change_reason=reason if events is None or not events else "",
            packet_id=self.complete_packet(),
        )
        return self.meta

    def test_stock_template_cleared_but_authored_notes_preserved(self):
        template = (
            "# Хронология — строго приватно\n\n"
            "## [ДД.ММ.ГГГГ]\n"
            "- [Только событие, повлиявшее на сюжет, знания, отношения, положение дел или важные предметы.]\n"
            "- [Время добавлять, только если оно важно.]\n\n"
            "Факты: что произошло на самом деле, без выдуманных трактовок. Не записывать бытовые действия без последствий. Частные убеждения персонажей — только в их `knowledge.md`. Записи можно уплотнять без удаления фактов и причин.\n"
        )
        event = [("02.05.2024", "Рекламный экран показал крышу Ринаты.")]
        result = _append_timeline(template, event)
        self.assertNotIn("[ДД.ММ.ГГГГ]", result)
        self.assertIn("## 02.05.2024", result)
        modified = template + "- Дополнительно установлено событие из прошлого.\n"
        kept = _append_timeline(modified, event)
        self.assertIn("Дополнительно установлено", kept)

    def test_no_implicit_timeline_decision_allowed(self):
        with self.assertRaisesRegex(StateError, "timeline_events"):
            self.store.commit_turn(
                self.sid, self.meta["version"], "читатель", "сцена",
                {"state/scene.md": "Новая сцена"}
            )
        with self.assertRaisesRegex(StateError, "почему хронология"):
            self.store.commit_turn(
                self.sid, self.meta["version"], "читатель", "сцена",
                {"state/scene.md": "Новая сцена"}, timeline_events=[]
            )
        self.assertEqual(self.store.read_state(self.sid)["turn"], 0)

    def test_auto_append_grouping_and_no_routine(self):
        self.save(
            "Экран показал крышу",
            events=["02.05.2024 | Рекламный экран показал крышу Ринаты."],
            reason="",
        )
        self.save("Рината посмотрела вокруг", events=[])
        self.save(
            "Появился дрон",
            events=[
                "02.05.2024 | Лестничная дверь приоткрылась и захлопнулась.",
                "02.05.2024 | Повреждённый дрон направил камеру на Ринату.",
            ],
            reason="",
        )
        text = self.files("state/timeline.md")
        self.assertEqual(text.count("## 02.05.2024"), 1)
        self.assertIn("Рекламный экран", text)
        self.assertIn("Повреждённый дрон", text)
        self.assertNotIn("посмотрела вокруг", text)
        self.assertEqual(self.meta["turn"], 3)
        self.assertIn("Появился дрон", self.files("state/recent.md"))

    def test_invalid_dates_and_manual_timeline_override_rejected(self):
        with self.assertRaises(StateError):
            self.save(events=["32.05.2024 | Такого дня нет."], reason="")
        with self.assertRaises(StateError):
            self.save(events=["Без даты был дрон."], reason="")
        with self.assertRaisesRegex(StateError, "автоматически"):
            self.store.commit_turn(
                self.sid, self.meta["version"], "читатель", "текст",
                {"state/scene.md": "Сцена", "state/timeline.md": "Подмена"},
                timeline_events=["02.05.2024 | Рекламный экран погас."],
            )
        self.assertEqual(self.store.read_state(self.sid)["turn"], 0)

    def test_recovery_from_existing_turns_without_gameplay_turn(self):
        first = self.save("Рекламный экран показал крышу", events=[])
        second = self.save("Дрон направил камеру на Ринату", events=[])
        old_recent = self.files("state/recent.md")
        original = self.store.read_original(self.sid)
        before = self.files("state/timeline.md")
        self.assertEqual(first["turn"], 1)
        self.assertEqual(second["turn"], 2)
        with self.assertRaisesRegex(StateError, "исходных ходов"):
            self.store.repair_timeline(
                self.sid, second["version"], [1, 3],
                ["02.05.2024 | Рекламный экран показал крышу."],
            )
        repaired = self.store.repair_timeline(
            self.sid, second["version"], [1, 2],
            [
                "02.05.2024 | Рекламный экран показал крышу.",
                "02.05.2024 | Повреждённый дрон направил камеру на Ринату.",
            ],
        )
        self.assertEqual(repaired["turn"], 2)
        self.assertGreater(repaired["version"], second["version"])
        self.assertFalse(repaired["gameplay_turn_added"])
        self.assertEqual(self.files("state/recent.md"), old_recent)
        self.assertEqual(self.store.read_original(self.sid), original)
        self.assertNotEqual(self.files("state/timeline.md"), before)
        self.assertIn("Рекламный экран", self.files("state/timeline.md"))
        repeat = self.store.repair_timeline(
            self.sid, repaired["version"], [1, 2],
            [
                "02.05.2024 | Рекламный экран показал крышу.",
                "02.05.2024 | Повреждённый дрон направил камеру на Ринату.",
            ],
        )
        self.assertTrue(repeat["already_present"])
        self.assertEqual(repeat["version"], repaired["version"])
        with self.assertRaisesRegex(StateError, "Версия устарела"):
            self.store.repair_timeline(
                self.sid, second["version"], [1, 2],
                ["02.05.2024 | Старые данные не перезаписывают новые."],
            )


if __name__ == "__main__":
    unittest.main()
