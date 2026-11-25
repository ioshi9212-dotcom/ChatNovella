"""Pre-save review must cover the current authored Skin Builder, not old scenes."""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novella_v2.engine import Engine, EngineError, empty_actor
from novella_v2.scene_preflight import (
    is_formatted_builder, review_sections, layout_errors, report_errors,
    review_items, rule_report_errors, messenger_layout_errors,
)


def state(turn):
    return {"turn": turn, "datetime": "2026-05-02T21:00:00",
            "location": "Комната", "pov": empty_actor(),
            "present": {"adrian": empty_actor()}, "remote": {},
            "objects": [], "unfinished": []}


def scene(turn=1):
    header = (
        "📖 Пока мир не сгорит · Весна\n"
        "🗓️ День 1 · суббота, 02.05.2026, 21:00\n"
        "📌 Комната · 🌤️ Погода: дождь 🎬 Сцена: Разговор\n"
        "👥 Участники сцены: Эдриан\n"
        "👤 Рината 👕 Джинсы 🎒 Телефон\n\n***\n\n"
    )
    # Synthetic, uninteresting prose is sufficient for deterministic layout tests.
    narration = "**Рината** — Привет. " + "Она оглянулась на шум за окном. " * 90
    footer = (
        "\n\n***\n\n"
        "💙 Что я могу сделать:\n- Пойти к двери\n- Проверить окно\n- Остаться\n\n"
        "🔹 Что я могу сказать:\n- Это кто?\n- Ты слышал?\n- Не подходи.\n\n"
        "🔸 Что я могу подумать:\n- Странно\n- Чёрт\n- Надо проверить\n\n"
        "***\n\nСостояние: настороженность\n"
        "Отношения: Эдриан — любовь 70/0.\n"
        f"Ход {turn} · цикл {((turn - 1) % 15) + 1}/15"
    )
    return header + narration + footer


class TestSceneReview(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.instructions = base / "instructions"
        self.instructions.mkdir()
        self.skin = (Path(__file__).resolve().parents[1]
                     / "skin_builder.md").read_text(encoding="utf-8")
        (self.instructions / "rules.md").write_text("Rules", encoding="utf-8")
        self.builder = self.instructions / "skin_builder.md"
        self.builder.write_text(self.skin, encoding="utf-8")
        self.engine = Engine(base / "sessions", self.instructions)
        original = "Мир. Эдриан знаком с Ринатой."
        split = original.index("Эдриан")
        result = self.engine.create(original, [
            {"start": 0, "end": split, "target": "novel"},
            {"start": split, "end": len(original), "target": "adrian"},
        ], {"adrian": "Эдриан"})
        self.sid = result["session_id"]
        self.engine.approve(self.sid, result["version"],
                            result["canon_hash"], "Утверждаю")
        self.engine.set_opening(self.sid, self.engine.status(self.sid)["version"],
                                state(0))

    def packet(self):
        p = self.engine.prepare(self.sid, "Привет", ["adrian"], request_id="review-1")
        for index in p["unread_chunk_indices"]:
            self.engine.read_chunk(self.sid, p["packet_id"], index)
        return p

    def checks(self):
        return [{"section": name, "result": "pass",
                 "note": "Проверены все применимые правила этого раздела."}
                for name in review_sections(self.skin)]

    def rule_checks(self):
        return [{"id": rule["id"], "result": "pass",
                 "note": "Есть буквальная цитата из проверяемой сцены.",
                 "evidence": "Она оглянулась на шум за окном."}
                for rule in review_items(self.skin)]

    def review(self, p, narration, checks=None, rule_checks=None):
        return self.engine.review_scene(
            self.sid, p["version"], p["packet_id"], "review-1",
            sha256(self.skin.encode("utf-8")).hexdigest(), narration,
            self.checks() if checks is None else checks,
            self.rule_checks() if rule_checks is None else rule_checks)

    def save(self, p, narration):
        return self.engine.commit(self.sid, {
            "request_id": "review-1", "expected_version": p["version"],
            "packet_id": p["packet_id"], "chunk_hashes": p["chunk_hashes"],
            "reader_text": "Привет", "narration": narration,
            "participants": ["adrian"], "state": state(1),
            "knowledge": [], "events": [], "relations": [],
        })

    def test_all_current_authored_sections_are_required(self):
        self.assertTrue(is_formatted_builder(self.skin))
        sections = review_sections(self.skin)
        self.assertGreaterEqual(len(sections), 10)
        self.assertIn("СЦЕНЫ ЗА КАДРОМ", sections)
        self.assertIn("ПРОВЕРКА ПЕРЕД ВЫВОДОМ", sections)
        self.assertIn("ПОЛНАЯ АВТОНОМИЯ НПС", sections)
        self.assertIn("КОНЕЦ СЦЕНЫ", sections)
        self.assertTrue(any("Не проверен раздел" in e
                            for e in report_errors(self.skin, self.checks()[:-1])))

    def test_messenger_plain_text_is_rejected_by_server(self):
        simple = "**Мариса** (23:45): Привет, ты дома?"
        self.assertTrue(messenger_layout_errors(self.skin, simple))
        formatted = "> **Мариса** (23:45)\n> Привет, ты дома?"
        self.assertEqual(messenger_layout_errors(self.skin, formatted), [])
        self.assertFalse(messenger_layout_errors(self.skin, "Телефон завибрировал."))

    def test_server_rejects_plain_sms_even_with_all_rules_marked_pass(self):
        p = self.packet()
        plain = scene().replace(
            "Она оглянулась на шум за окном. ",
            "**Мариса** (23:45): Привет, ты дома?\n", 1)
        rejected = self.review(p, plain)
        self.assertFalse(rejected["approved"])
        self.assertTrue(any("мессенджера" in e for e in rejected["issues"]))
        self.assertEqual(self.engine.status(self.sid)["turn"], 0)
        boxed = scene().replace(
            "Она оглянулась на шум за окном. ",
            "> **Мариса** (23:45)\n> Привет, ты дома?\n", 1)
        approved = self.review(p, boxed)
        self.assertTrue(approved["approved"], approved["issues"])

    def test_cannot_skip_individual_rule_or_fake_evidence(self):
        self.assertGreater(len(review_items(self.skin)), 50)
        authored = [item["text"] for item in review_items(self.skin)]
        self.assertTrue(any("АВТОНОМИЯ ПОВ:" in text for text in authored))
        self.assertTrue(any("ОТВЕТ ЧИТАТЕЛЯ:" in text for text in authored))
        items = self.rule_checks()
        self.assertTrue(any("Не проверено правило" in x for x in
                            rule_report_errors(self.skin, scene(), items[:-1])))
        items[0]["evidence"] = "В сцене нет такой цитаты."
        self.assertTrue(any("Нет точной цитаты" in x for x in
                            rule_report_errors(self.skin, scene(), items)))

    def test_formatted_scene_passes_layout(self):
        self.assertEqual(layout_errors(self.skin, scene(), 1), [])

    def test_missing_chat_format_header_choices_and_volume_are_detected(self):
        errors = layout_errors(self.skin, "Просто текст", 1)
        self.assertTrue(any("Шапка" in e for e in errors))
        self.assertTrue(any("Отсутствует блок" in e for e in errors))
        bad = scene().replace("🔹 Что я могу сказать:", "Реплики:")
        self.assertTrue(layout_errors(self.skin, bad, 1))
        short = scene().replace("Она оглянулась на шум за окном. " * 90, "Тишина.")
        self.assertTrue(any("Объём" in e for e in layout_errors(self.skin, short, 1)))

    def test_unreviewed_scene_cannot_be_saved(self):
        p = self.packet()
        with self.assertRaisesRegex(EngineError, "Review the exact final scene"):
            self.save(p, scene())
        self.assertEqual(self.engine.status(self.sid)["turn"], 0)

    def test_invalid_report_does_not_approve_and_revised_text_must_be_rechecked(self):
        p = self.packet()
        fail = self.review(p, scene(), self.checks()[:-1])
        self.assertFalse(fail["approved"])
        self.assertTrue(fail["issues"])
        self.assertFalse(self.review(p, "Слишком коротко")["approved"])
        self.assertEqual(self.engine.status(self.sid)["turn"], 0)

        passed = self.review(p, scene())
        self.assertTrue(passed["approved"])
        with self.assertRaisesRegex(EngineError, "Review the exact final scene"):
            self.save(p, scene().replace("Странно", "Дерьмо"))
        self.assertEqual(self.engine.status(self.sid)["turn"], 0)
        result = self.save(p, scene())
        self.assertTrue(result["saved"])
        self.assertEqual(self.engine.status(self.sid)["turn"], 1)

    def test_explicit_builder_version_binding(self):
        p = self.packet()
        self.builder.write_text(self.skin + "\nНовый пункт: проверять внимательнее.",
                                encoding="utf-8")
        with self.assertRaisesRegex(EngineError, "Refresh the active Skin Builder"):
            self.review(p, scene())
        self.assertEqual(self.engine.status(self.sid)["turn"], 0)


if __name__ == "__main__":
    unittest.main()
