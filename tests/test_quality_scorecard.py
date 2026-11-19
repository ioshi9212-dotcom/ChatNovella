"""Quality scorecard verifies excerpts and blocks fatal defects before save."""
import copy
import unittest

from chatnovella.quality_scorecard import check_scorecard, narrative_body, copied_prose, MIN_SCORE
from review_helpers import scene, scorecard


class TestQualityScorecard(unittest.TestCase):
    def test_real_excerpts_and_clean_12_pass(self):
        issues, result = check_scorecard(scorecard(), scene(), "(Оглядеться)")
        self.assertEqual(issues, [])
        self.assertEqual(result["total"], 12)
        self.assertFalse(result["critical"])

    def test_missing_or_fabricated_evidence_cannot_pass(self):
        for draft in [None, {}, {"axes": {}}, "Я всё проверил"]:
            issues, _ = check_scorecard(draft, scene(), "(Оглядеться)")
            self.assertTrue(issues)
        report = scorecard()
        report["axes"]["pacing"]["quote"] = "Выдуманная фраза из художественной части."
        issues, _ = check_scorecard(report, scene(), "(Оглядеться)")
        self.assertTrue(any("scorecard.pacing" in issue for issue in issues))
        report = scorecard()
        report["axes"]["pacing"]["quote"] = report["axes"]["plot_initiative"]["quote"]
        report["axes"]["genre_detail"]["quote"] = report["axes"]["plot_initiative"]["quote"]
        report["axes"]["no_blurring_or_fade"]["quote"] = report["axes"]["plot_initiative"]["quote"]
        issues, _ = check_scorecard(report, scene(), "(Оглядеться)")
        self.assertTrue(any("scorecard.evidence" in issue for issue in issues))

    def test_threshold_is_computed_by_server_not_author(self):
        report = scorecard()
        for key in ("genre_detail", "pacing"):
            report["axes"][key]["score"] = 1
            report["axes"][key]["issue"] = "В этом месте заметен нераскрытый момент сценического действия."
            report["axes"][key]["fix"] = "Нужно уточнить последовательность поступков и убрать лишний повтор."
        issues, result = check_scorecard(report, scene(), "(Оглядеться)")
        self.assertEqual(result["total"], 10)
        self.assertTrue(any("scorecard.total" in issue for issue in issues))
        report["axes"]["pacing"]["score"] = 2
        report["axes"]["pacing"]["issue"] = ""
        report["axes"]["pacing"]["fix"] = ""
        issues, result = check_scorecard(report, scene(), "(Оглядеться)")
        self.assertEqual(result["total"], MIN_SCORE)
        self.assertEqual(issues, [])

    def test_critical_error_blocks_even_twelve(self):
        report = scorecard()
        report["critical"]["pov_choice"]["violation"] = True
        report["critical"]["pov_choice"]["reason"] = (
            "Писатель заставил Ринату сделать принципиальный выбор без команды игрока."
        )
        issues, summary = check_scorecard(report, scene(), "(Оглядеться)")
        self.assertEqual(summary["total"], 12)
        self.assertTrue(summary["critical"])
        self.assertTrue(any("scorecard.critical.pov_choice" in item for item in issues))

    def test_time_fade_requires_concrete_explanation(self):
        body = scene().replace(
            "За стеллажом скрипнула тяжёлая дверь",
            "Спустя несколько часов за стеллажом скрипнула тяжёлая дверь",
        )
        report = scorecard()
        report["axes"]["npc_character_initiative"]["quote"] = (
            "Спустя несколько часов за стеллажом скрипнула тяжёлая дверь без видимого толчка."
        )
        issues, summary = check_scorecard(report, body, "(Оглядеться)")
        self.assertTrue(any("scorecard.fade:" in issue for issue in issues))
        self.assertTrue(summary["fade_cues"])
        report["fade_notes"] = [{
            "quote": "Спустя несколько часов",
            "reason": "В этот промежуток не было незавершённого важного события, переход сохраняет причинность и не скрывает действий.",
        }]
        issues, summary = check_scorecard(report, body, "(Оглядеться)")
        self.assertEqual(issues, [])

    def test_reader_trace_is_checked_against_actual_input(self):
        report = scorecard()
        report["reader_trace"]["input_quote"] = "(Подойти и обнять человека)"
        issues, _ = check_scorecard(report, scene(), "(Оглядеться)")
        self.assertTrue(any("scorecard.reader_trace" in issue for issue in issues))

    def test_substantial_copy_from_previous_scene_is_detected(self):
        repeated = scene()
        self.assertTrue(copied_prose(repeated, [repeated]))
        self.assertEqual(copied_prose(repeated, []), [])
        different = scene().replace(
            "В дальнем углу хлопнула дверь.", "За окном раздался короткий звон."
        )
        self.assertEqual(copied_prose(different, [scene()]), [])

    def test_bools_do_not_count_as_scores(self):
        report = scorecard()
        report["axes"]["pacing"]["score"] = True
        issues, _ = check_scorecard(report, scene(), "(Оглядеться)")
        self.assertTrue(any("scorecard.pacing" in issue for issue in issues))

    def test_markdown_titles_and_emphasis_do_not_hide_real_scene_evidence(self):
        text = scene()
        report = scorecard()
        for key, item in report["axes"].items():
            quote = item["quote"]
            text = text.replace(quote, "**" + quote + "**")
            item["quote"] = "«" + quote + "»"
        for label in ("Что я могу сделать:", "Что я могу сказать:", "Что я могу подумать:"):
            text = text.replace(label, "### 🔸 **" + label.rstrip(":").replace(" ", "\u00a0") + "**")
        issues, result = check_scorecard(report, text, "(Оглядеться)")
        self.assertEqual(issues, [])
        self.assertEqual(result["total"], 12)
        self.assertNotIn("Что я могу сделать", narrative_body(text))
        report["axes"]["pacing"]["quote"] = "«На полу у порога лежала совершенно другая выдуманная вещь.»"
        issues, _ = check_scorecard(report, text, "(Оглядеться)")
        self.assertTrue(any("scorecard.pacing" in issue for issue in issues))

    def test_quotes_from_options_and_markup_only_are_not_evidence(self):
        report = scorecard()
        text = scene().replace("Что я могу сделать:", "## 🔸 _Что я могу сделать_")
        for quote in ("********************", "Пора найти нужный стол."):
            report["axes"]["pacing"]["quote"] = quote
            issues, _ = check_scorecard(report, text, "(Оглядеться)")
            self.assertTrue(any("scorecard.pacing" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
