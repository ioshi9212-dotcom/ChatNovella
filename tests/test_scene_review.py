"""Review gates reject actual bad drafts, enforce the budget and bind the save."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from chatnovella.scene_review import REVIEW_CHECKS, inspect_scene
from chatnovella.store import CHAR_FILES, STATE_FILES, SessionStore, StateError
from review_helpers import checks, scene


class TestSceneReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        template = self.base / "template"
        for name in STATE_FILES:
            file = template / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("Начальное состояние", encoding="utf-8")
        self.template = template
        self.store = SessionStore(self.base / "data", template)
        meta = self.store.create_session("Оригинал дословно\n", "Канон", "Рината")
        self.sid = meta["session_id"]
        self.meta = self.store.activate(self.sid, meta["version"], True)

    def preflight(self, reader_text="(Оглядеться)"):
        scout = self.store.prepare_turn(self.sid, [])
        for index in range(scout["count"]):
            self.store.read_chunk(scout["packet_id"], index)
        self.store.plan_scene(self.sid, self.meta["version"], reader_text, scout["packet_id"], {
            "basis": "Рината находится в читальном зале и осматривается",
            "active_arcs": [], "candidate_npcs": [],
            "time_span": "Короткий промежуток времени без долгого ожидания",
            "npc_initiative": "Появление NPC не требуется, окружение остаётся живым",
            "intended_shift": "Рината замечает необычное происшествие в зале",
            "possible_hook": "Обнаруженный след у порога допускает продолжение",
        })

    def payload(self, participants=None):
        self.preflight()
        packet = self.store.prepare_turn(self.sid, participants or [])
        for index in range(packet["count"]):
            self.store.read_chunk(packet["packet_id"], index)
        return dict(
            session_id=self.sid, expected_version=self.meta["version"],
            reader_text="(Оглядеться)", narration=scene(),
            files={"state/scene.md": "Рината стоит у двери читального зала."},
            timeline_events=[], timeline_no_change_reason="Осмотр зала не изменил сюжетных фактов",
            packet_id=packet["packet_id"],
        )

    def review(self, payload, report=None, labels=None):
        return self.store.review_scene(**payload, checks=report or checks(),
                                       relationship_labels=labels or {})

    def test_review_rejects_missing_pre_scene_plan(self):
        packet = self.store.prepare_turn(self.sid, [])
        for index in range(packet["count"]):
            self.store.read_chunk(packet["packet_id"], index)
        with self.assertRaisesRegex(StateError, "plan_novella_scene"):
            self.store.review_scene(
                session_id=self.sid, expected_version=self.meta["version"],
                reader_text="(Посмотреть в окно)", narration=scene(),
                files={"state/scene.md": "Рината изучила обстановку."},
                timeline_events=[], timeline_no_change_reason="Нет значимого изменения",
                packet_id=packet["packet_id"], checks=checks(), relationship_labels={},
            )

    def test_world_scout_includes_npc_before_participant_selection(self):
        self.add_npc()
        packet = self.store.prepare_turn(self.sid, [])
        whole = "".join(self.store.read_chunk(packet["packet_id"], index)["text"]
                        for index in range(packet["count"]))
        self.assertIn("system/npc_scout", whole)
        self.assertIn("NPC_001", whole)
        self.assertIn("Подтверждённый персонаж Сайлас", whole)

    def test_save_requires_approval_for_exact_text_files_chronology_and_packet(self):
        payload = self.payload()
        before = self.store.read_state(self.sid)
        with self.assertRaisesRegex(StateError, "review_novella_scene"):
            self.store.commit_turn(**payload)
        result = self.review(payload)
        self.assertTrue(result["approved"], result)
        self.assertEqual(self.store.read_state(self.sid), before)
        for field, value in (
            ("narration", scene("Рината открыла окно.")),
            ("reader_text", "Другой ввод"),
            ("files", {"state/scene.md": "Она уже у окна."}),
            ("timeline_events", ["02.05.2024 | Окно оказалось заперто снаружи."]),
            ("timeline_no_change_reason", "Другое объяснение отсутствия событий"),
            ("packet_id", self.payload()["packet_id"]),
        ):
            changed = copy.deepcopy(payload)
            changed[field] = value
            if field == "timeline_events":
                changed["timeline_no_change_reason"] = ""
            with self.subTest(field=field), self.assertRaises(StateError):
                self.store.commit_turn(**changed, review_id=result["review_id"])
            self.assertEqual(self.store.read_state(self.sid), before)
        saved = self.store.commit_turn(**payload, review_id=result["review_id"])
        self.assertEqual(saved["turn"], 1)
        rev = self.store._revision(self.store._root(self.sid), saved["version"])
        entry = json.loads((rev / "system/turns.jsonl").read_text())
        self.assertEqual(entry["narration"], payload["narration"])
        self.assertEqual(set(entry["scene_review"]["checks"]), set(REVIEW_CHECKS))
        self.assertEqual(self.store.read_original(self.sid), "Оригинал дословно\n")

    def test_initial_draft_plus_two_corrections_can_succeed(self):
        payload = self.payload()
        payload["narration"] = scene().replace("📖", "Книга", 1)
        first = self.review(payload)
        self.assertFalse(first["approved"])
        self.assertEqual((first["correction_pass"], first["corrections_remaining"]), (0, 2))
        payload["narration"] = scene().replace("- Проверить дверь\n", "")
        second = self.review(payload)
        self.assertFalse(second["approved"])
        self.assertEqual((second["correction_pass"], second["corrections_remaining"]), (1, 1))
        payload["narration"] = scene()
        third = self.review(payload)
        self.assertTrue(third["approved"], third)
        self.assertEqual((third["correction_pass"], third["corrections_remaining"]), (2, 0))
        self.assertEqual(self.store.read_state(self.sid)["turn"], 0)
        saved = self.store.commit_turn(**payload, review_id=third["review_id"])
        self.assertEqual(saved["turn"], 1)

    def test_budget_survives_packet_refresh_restart_and_retries(self):
        payload = self.payload()
        for attempt in range(3):
            payload["narration"] = scene(f"Проверочный черновик {attempt}").replace("📖", "Книга", 1)
            result = self.review(payload)
            self.assertEqual(result["correction_pass"], attempt)
            self.assertEqual(self.review(payload), result)
        self.assertTrue(result["blocked"])
        self.store = SessionStore(self.base / "data", self.template)
        fresh = self.payload()
        blocked = self.review(fresh)
        self.assertFalse(blocked["approved"])
        self.assertTrue(blocked["blocked"])
        self.assertIsNone(blocked["review_id"])
        self.assertEqual(blocked["corrections_remaining"], 0)
        self.assertEqual(self.store.read_state(self.sid), self.meta)
        # An actual new reader message allows a new cycle; packet refresh alone did not.
        fresh["reader_text"] = "(Проверить окно вместо осмотра зала)"
        self.preflight(fresh["reader_text"])
        allowed = self.review(fresh)
        self.assertTrue(allowed["approved"], allowed)
        self.assertEqual(allowed["correction_pass"], 0)

    def test_reported_semantic_failure_blocks_and_old_approval_is_revoked(self):
        payload = self.payload()
        approved = self.review(payload)
        report = checks()
        report["pov"] = {"status": "fail", "evidence": "После первой фразы POV молчит весь разговор без причины по её характеру."}
        failed = self.review(payload, report)
        self.assertFalse(failed["approved"])
        self.assertTrue(any("POV молчит" in issue for issue in failed["issues"]))
        with self.assertRaises(StateError):
            self.store.commit_turn(**payload, review_id=approved["review_id"])
        payload["narration"] = scene("**Рината** — Здесь кто-нибудь есть?\nОна подошла ближе к двери.")
        repaired = self.review(payload)
        self.assertTrue(repaired["approved"], repaired)
        self.assertEqual(repaired["correction_pass"], 2)

    def test_complete_context_is_required_before_review_old_packet_is_rejected(self):
        payload = self.payload()
        packet = self.store.prepare_turn(self.sid, [])
        payload["packet_id"] = packet["packet_id"]
        with self.assertRaisesRegex(StateError, "чанки"):
            self.review(payload)
        for index in range(packet["count"]):
            self.store.read_chunk(packet["packet_id"], index)
        path = self.store.packets / f"{packet['packet_id']}.json"
        data = json.loads(path.read_text())
        data.pop("review_contract")
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(StateError, "новый prepare_novella_turn"):
            self.review(payload)

    def test_mechanical_checks_reject_missing_blocks_bad_options_and_placeholders(self):
        bad_scenes = {
            "header": scene().replace("📖", "Книга", 1),
            "date": scene().replace("02.05.2024", "32.05.2024"),
            "placeholder": scene().replace("Читальный зал", "[место]"),
            "missing_option": scene().replace("- Проверить дверь\n", ""),
            "four_options": scene().replace("- Проверить дверь\n", "- Проверить дверь\n- Уйти на улицу\n"),
            "duplicate_options": scene().replace("- Проверить дверь", "- Открыть окно"),
            "quotes": scene().replace("- Кто здесь?", "- «Кто здесь?»"),
            "state": scene().replace("спокойна, внимательна", "слово " * 11),
            "footer": scene().replace("Отношения:", "Связи:"),
            "unnamed_speaker": scene("— Кто здесь?"),
        }
        for name, text in bad_scenes.items():
            with self.subTest(name=name):
                issues, _ = inspect_scene(text, checks(), {})
                self.assertTrue(issues, name)
        issues, _ = inspect_scene(scene(), checks(), {})
        self.assertEqual(issues, [])

    def test_direction_requires_specific_progress_and_hook_evidence(self):
        report = checks()
        report["direction"]["evidence"] = "Сцена получилась интересной, в конце есть варианты."
        issues, _ = inspect_scene(scene(), report, {})
        self.assertTrue(any("review.direction" in issue for issue in issues))

        report["direction"]["evidence"] = (
            "Опора: ситуация в читальном зале; "
            "Сдвиг: В дальнем углу хлопнула дверь; "
            "Зацепка: Рината заметила свежий след у порога"
        )
        issues, _ = inspect_scene(scene(), report, {})
        self.assertEqual(issues, [])

    def test_empty_generic_or_missing_semantic_results_cannot_pass(self):
        for report in ({}, {key: {"status": "pass", "evidence": "Все проверено"} for key in REVIEW_CHECKS},
                       {key: {"status": "pass", "evidence": "Одинаковое заверение во всех пунктах"} for key in REVIEW_CHECKS}):
            with self.subTest(report=report):
                issues, _ = inspect_scene(scene(), report, {})
                self.assertTrue(issues)

    def add_npc(self):
        files = {f"characters/NPC_001/{name}": "Подтверждённый персонаж Сайлас"
                 for name in CHAR_FILES}
        files["characters/NPC_001/relations.md"] = "- интерес 50/+2\n- доверие 30/+0"
        self.meta = self.store.update_setup(self.sid, self.meta["version"], files)
        self.meta = self.store.activate(self.sid, self.meta["version"], True)

    def test_footer_uses_real_scales_and_zero_delta_when_card_is_unchanged(self):
        self.add_npc()
        payload = self.payload([{"id": "NPC_001", "mode": "observer"}])
        labels = {"NPC_001": "Сайлас"}
        payload["narration"] = scene(relationships="Сайлас — интерес 50/+2; доверие 30/0")
        wrong = self.review(payload, labels=labels)
        self.assertFalse(wrong["approved"])
        self.assertTrue(any("точным итогом" in issue for issue in wrong["issues"]))
        payload["narration"] = scene(relationships="Сайлас — интерес 50/0; доверие 30/0")
        right = self.review(payload, labels=labels)
        self.assertTrue(right["approved"], right)

    def test_missing_scales_or_participant_cannot_hide_behind_pass_report(self):
        self.add_npc()
        payload = self.payload([{"id": "NPC_001", "mode": "phone"}])
        payload["narration"] = scene(relationships="Сайлас — интерес 50/0")
        result = self.review(payload, labels={"NPC_001": "Сайлас"})
        self.assertFalse(result["approved"])
        payload["narration"] = scene()
        missing = self.review(payload)
        self.assertFalse(missing["approved"])
        self.assertTrue(any("всех участвующих NPC" in issue for issue in missing["issues"]))
        payload["narration"] = scene(relationships="Сайлас — интерес 52/+2; доверие 30/0")
        payload["files"]["characters/NPC_001/relations.md"] = "- интерес 52/+2\n- доверие 30/+0"
        right = self.review(payload, labels={"NPC_001": "Сайлас"})
        self.assertTrue(right["approved"], right)

    def test_review_cannot_authorize_another_session(self):
        payload = self.payload()
        result = self.review(payload)
        other = self.store.create_session("Другая новелла", "Иной канон", "Другой POV")
        other = self.store.activate(other["session_id"], other["version"], True)
        packet = self.store.prepare_turn(other["session_id"], [])
        for index in range(packet["count"]):
            self.store.read_chunk(packet["packet_id"], index)
        payload.update(session_id=other["session_id"], expected_version=other["version"], packet_id=packet["packet_id"])
        with self.assertRaisesRegex(StateError, "Нет проверки"):
            self.store.commit_turn(**payload, review_id=result["review_id"])


if __name__ == "__main__":
    unittest.main()
