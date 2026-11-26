"""Pre-save review must cover the current authored Skin Builder, not old scenes."""
from __future__ import annotations

from hashlib import sha256
import json
from unittest.mock import patch
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
        self.engine = Engine(base / "sessions", self.instructions, scene_review_enabled=True)
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
        self.engine.director_brief(self.sid)
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
        # Author may revise headings at any time. Test the parser against
        # current text rather than pinning an old author's layout forever.
        self.assertTrue(sections)
        self.assertIn("ФОРМАТ СЦЕНЫ", sections)
        self.assertEqual(len(sections), len(set(sections)))
        sample = "ФОРМАТ СЦЕНЫ 📖\nСЦЕНЫ ЗА КАДРОМ: правило\n### 11. ПРОВЕРКА ПЕРЕД ВЫВОДОМ"
        self.assertEqual(review_sections(sample),
                         ["ФОРМАТ СЦЕНЫ", "СЦЕНЫ ЗА КАДРОМ", "ПРОВЕРКА ПЕРЕД ВЫВОДОМ"])
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
        original_lines = self.skin.splitlines()
        authored = review_items(self.skin)
        self.assertTrue(all(item["text"] == original_lines[int(item["id"][1:]) - 1].strip()
                            for item in authored))
        sample = "АВТОНОМИЯ ПОВ: уважать ввод.\nОТВЕТ ЧИТАТЕЛЯ: не менять его."
        self.assertEqual([item["text"] for item in review_items(sample)], sample.splitlines())
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

    def game_files(self):
        root = self.engine.root / self.sid
        return {str(p.relative_to(root)): p.read_bytes()
                for p in root.rglob("*") if p.is_file()
                and "system" not in p.relative_to(root).parts}

    def npc_packet(self, source="Маркус вошёл в комнату.", request_id="review-1"):
        self.engine.director_brief(self.sid)
        p = self.engine.prepare(self.sid, "Привет", ["adrian", "marcus"],
            request_id=request_id, new_characters=[{
                "name": "marcus", "display": "Маркус", "established_text": source}])
        for index in p["unread_chunk_indices"]:
            self.engine.read_chunk(self.sid, p["packet_id"], index)
        return p

    def npc_payload(self, p, narration, used=True):
        current = state(1)
        people = ["adrian"]
        knowledge, events = [], []
        if used:
            current["present"]["marcus"] = empty_actor()
            people.append("marcus")
            knowledge.append({"owner": "marcus", "fact": "Вошёл в комнату",
                              "source": "Маркус вошёл в комнату."})
            events.append({"text": "Маркус вошёл в комнату.", "participants": ["marcus"]})
        return {"request_id": "review-1", "expected_version": p["version"],
                "packet_id": p["packet_id"], "chunk_hashes": p["chunk_hashes"],
                "reader_text": "Привет", "narration": narration,
                "participants": people, "state": current, "knowledge": knowledge,
                "events": events, "relations": []}

    def test_new_npc_and_all_game_files_unchanged_until_reviewed_commit(self):
        before = self.game_files()
        version = self.engine.status(self.sid)["version"]
        proposal = self.engine.add_character(self.sid, version, "marcus", "Маркус",
                                              "Маркус вошёл в комнату.")
        self.assertFalse(proposal["added"])
        self.assertEqual(self.game_files(), before)
        p = self.npc_packet()
        self.assertEqual(self.game_files(), before)
        self.assertNotIn("marcus", self.engine._cast(self.engine.root / self.sid))
        full = scene().replace("**Рината** — Привет.", "Маркус вошёл в комнату. **Рината** — Привет.")
        data = self.npc_payload(p, full)
        with self.assertRaisesRegex(EngineError, "Review the exact final scene"):
            self.engine.commit(self.sid, data)
        self.assertEqual(self.game_files(), before)
        failed = self.review(p, "Маркус вошёл в комнату.")
        self.assertFalse(failed["approved"])
        self.assertEqual(self.game_files(), before)
        approved = self.review(p, full)
        self.assertTrue(approved["approved"], approved)
        self.assertEqual(self.game_files(), before)
        self.assertEqual(self.engine.status(self.sid)["version"], version)
        saved = self.engine.commit(self.sid, data)
        self.assertTrue(saved["saved"])
        after = self.game_files()
        self.assertIn(b"marcus", after["novel.md"])
        self.assertIn("characters/marcus.md", after)
        self.assertIn("knowledge/marcus.md", after)
        self.assertIn("Маркус вошёл".encode(), after["timeline.md"])
        self.assertEqual(self.engine.commit(self.sid, data), saved)
        self.engine.rollback(self.sid, saved["version"], "review-1", "Отмена знакомства")
        restored = self.game_files()
        self.assertNotIn("characters/marcus.md", restored)
        self.assertNotIn("knowledge/marcus.md", restored)
        self.assertEqual({k:v for k,v in restored.items() if k != "history.jsonl"},
                         {k:v for k,v in before.items() if k != "history.jsonl"})
        self.assertIn(b'"kind":"rollback"', restored["history.jsonl"])
        self.assertEqual(self.engine.status(self.sid)["turn"], 0)

    def test_abandoned_temporary_npc_never_registers(self):
        before = self.game_files()
        p = self.npc_packet()
        full = scene()
        self.assertTrue(self.review(p, full)["approved"])
        self.assertTrue(self.engine.commit(self.sid, self.npc_payload(p, full, used=False))["saved"])
        after = self.game_files()
        self.assertEqual(after["novel.md"], before["novel.md"])
        self.assertNotIn("characters/marcus.md", after)
        self.assertNotIn("knowledge/marcus.md", after)

    def test_new_npc_requires_actual_approved_source(self):
        before = self.game_files()
        p = self.npc_packet()
        full = scene()
        self.assertTrue(self.review(p, full)["approved"])
        with self.assertRaisesRegex(EngineError, "exact excerpt"):
            self.engine.commit(self.sid, self.npc_payload(p, full))
        self.assertEqual(self.game_files(), before)

    def test_new_npc_final_quote_can_change_without_reprepare(self):
        p = self.npc_packet(source="Маркус подошёл к окну.")
        full = scene().replace("**Рината** — Привет.", "Маркус вошёл в комнату. **Рината** — Привет.")
        self.assertTrue(self.review(p, full)["approved"])
        data = self.npc_payload(p, full)
        data["new_character_evidence"] = {"marcus": "Маркус вошёл в комнату."}
        self.assertTrue(self.engine.commit(self.sid, data)["saved"])
        card = self.game_files()["characters/marcus.md"].decode()
        self.assertIn("Маркус вошёл в комнату.", card)
        self.assertNotIn("Маркус подошёл к окну.", card)

    def test_new_npc_rollback_recovers_interrupted_deletion(self):
        p = self.npc_packet()
        full = scene().replace("**Рината** — Привет.", "Маркус вошёл в комнату. **Рината** — Привет.")
        self.assertTrue(self.review(p, full)["approved"])
        saved = self.engine.commit(self.sid, self.npc_payload(p, full))
        from novella_v2.engine import remove_character_file
        deleted = []
        def fail_once(path, name):
            remove_character_file(path, name)
            deleted.append(name)
            if len(deleted) == 1:
                raise OSError("synthetic power loss")
        with patch("novella_v2.engine.remove_character_file", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "synthetic power loss"):
                self.engine.rollback(self.sid, saved["version"], "review-1", "Отмена")
        restarted = Engine(self.engine.root, self.instructions)
        self.assertEqual(restarted.status(self.sid)["turn"], 0)
        self.assertFalse((self.engine.root / self.sid / "characters/marcus.md").exists())
        self.assertFalse((self.engine.root / self.sid / "knowledge/marcus.md").exists())
        self.assertNotIn("marcus", restarted._cast(self.engine.root / self.sid))

    def test_default_disabled_saves_scene_without_literary_review(self):
        with patch.dict("os.environ", {}, clear=True):
            self.engine = Engine(self.engine.root, self.instructions)
        self.assertFalse(self.engine.scene_review_enabled)
        before = self.game_files()
        with patch("novella_v2.scene_preflight.review_items",
                   side_effect=AssertionError("Disabled review must not build a report")), \
             patch("novella_v2.scene_preflight.layout_errors",
                   side_effect=AssertionError("Disabled review must not check layout")):
            p = self.packet()
            self.assertFalse(p["scene_review_enabled"])
            payload = json.loads((self.engine.root / self.sid / "system/packet.json").read_text())
            self.assertFalse(payload["skin_builder"]["scene_review_enabled"])
            self.assertNotIn("review_rule_ids", payload["skin_builder"])
            self.assertNotIn("review_sections", payload["skin_builder"])
            self.assertEqual(payload["skin_builder"]["content"], self.skin)
            self.assertEqual(list(payload)[-1], "skin_builder")
            result = self.engine.review_scene(self.sid, p["version"], p["packet_id"],
                       "review-1", p["skin_sha256"], "Короткая сцена.", [], [])
            self.assertTrue(result["skipped"])
            self.assertFalse(result["approved"])
            self.assertEqual(self.game_files(), before)
            saved = self.save(p, "Короткая сцена.")
            self.assertTrue(saved["saved"])
            self.assertEqual(saved["turn"], 1)

    def test_disabled_new_npc_still_requires_final_scene_evidence(self):
        self.engine.scene_review_enabled = False
        before = self.game_files()
        p = self.npc_packet()
        with self.assertRaisesRegex(EngineError, "exact excerpt"):
            self.engine.commit(self.sid, self.npc_payload(p, "Маркус молчал."))
        self.assertEqual(self.game_files(), before)
        result = self.engine.commit(self.sid, self.npc_payload(p, "Маркус вошёл в комнату."))
        self.assertTrue(result["saved"])
        self.assertIn("characters/marcus.md", self.game_files())
        self.engine.rollback(self.sid, result["version"], "review-1", "Отмена")
        self.assertNotIn("characters/marcus.md", self.game_files())

    def test_reenable_review_requires_fresh_packet_and_real_receipt(self):
        self.engine.scene_review_enabled = False
        old = self.packet()
        with patch.dict("os.environ", {"CHATNOVELLA_V2_SCENE_REVIEW": "1"}):
            self.engine = Engine(self.engine.root, self.instructions)
        self.assertTrue(self.engine.scene_review_enabled)
        with self.assertRaisesRegex(EngineError, "current review mode"):
            self.save(old, scene())
        fresh = self.packet()
        self.assertNotEqual(fresh["packet_id"], old["packet_id"])
        self.assertTrue(fresh["scene_review_enabled"])
        with self.assertRaisesRegex(EngineError, "Review the exact final scene"):
            self.save(fresh, scene())
        self.assertTrue(self.review(fresh, scene())["approved"])
        self.assertTrue(self.save(fresh, scene())["saved"])

    def test_disabled_review_does_not_bypass_chunk_or_instruction_integrity(self):
        self.engine.scene_review_enabled = False
        self.engine.director_brief(self.sid)
        p = self.engine.prepare(self.sid, "Привет", ["adrian"], request_id="review-1")
        with self.assertRaisesRegex(EngineError, "Read and verify all"):
            self.save(p, "Сцена.")
        for index in p["unread_chunk_indices"]:
            self.engine.read_chunk(self.sid, p["packet_id"], index)
        self.builder.write_text(self.skin + "\nНовая версия.", encoding="utf-8")
        with self.assertRaisesRegex(EngineError, "Refresh the active Skin Builder"):
            self.save(p, "Сцена.")
        self.assertEqual(self.engine.status(self.sid)["turn"], 0)

    def test_explicit_builder_version_binding(self):
        p = self.packet()
        self.builder.write_text(self.skin + "\nНовый пункт: проверять внимательнее.",
                                encoding="utf-8")
        with self.assertRaisesRegex(EngineError, "Refresh the active Skin Builder"):
            self.review(p, scene())
        self.assertEqual(self.engine.status(self.sid)["turn"], 0)


if __name__ == "__main__":
    unittest.main()
