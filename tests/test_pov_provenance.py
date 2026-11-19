"""Check that significant decisions of POV need reader or already-started origins."""
import unittest

from chatnovella.pov_provenance import detect_pov_decisions, validate_pov_provenance
from chatnovella.quality_scorecard import check_scorecard
from review_helpers import scene, scorecard


class TestPovProvenance(unittest.TestCase):
    def setUp(self):
        self.body = (
            "Рина уже была рядом. Она коснулась его настоящей тёплой руки "
            "и обняла Эдриана."
        )

    def test_embrace_without_reader_instruction_is_critical_fail(self):
        report = scorecard("(Вернуться в школу)")
        issues, result = check_scorecard(report, scene(self.body), "(Вернуться в школу)")
        self.assertEqual(result["pov_consequential_actions"], 1)
        self.assertTrue(result["critical"])
        self.assertTrue(any("обняла" in issue for issue in issues), issues)

    def test_real_reader_permission_allows_the_embrace(self):
        reader = "(Вернуться к Эдриану и обнять его)"
        report = scorecard(reader)
        report["pov_actions"] = [{
            "scene_quote": "Она коснулась его настоящей тёплой руки и обняла Эдриана.",
            "source": "reader",
            "source_quote": "обнять его",
            "reason": "Читатель сам велел Ринате обнять Эдриана; описание только выполняет это действие.",
        }]
        issues, result = check_scorecard(report, scene(self.body), reader)
        self.assertEqual(issues, [], issues)
        self.assertFalse(result["critical"])

    def test_unrelated_reader_phrase_cannot_authorize_a_hug(self):
        reader = "(Вернуться в зал и оглянуться)"
        report = scorecard(reader)
        report["pov_actions"] = [{
            "scene_quote": "Она коснулась его настоящей тёплой руки и обняла Эдриана.",
            "source": "reader",
            "source_quote": "Вернуться в зал",
            "reason": "Автор посчитал объятие естественным дополнением возвращения в зал, но это новый выбор.",
        }]
        issues, _ = check_scorecard(report, scene(self.body), reader)
        self.assertTrue(any("не подтверждают одно и то же" in issue for issue in issues))
        self.assertTrue(any("обняла" in issue for issue in issues))

    def test_npc_embrace_not_confused_with_pov_action(self):
        body = "Эдриан обнял Ринату. Рина удивлённо подняла глаза и выдохнула."
        self.assertEqual(detect_pov_decisions(body), [])
        issues, _ = check_scorecard(scorecard(), scene(body), "(Оглядеться)")
        self.assertEqual(issues, [], issues)

    def test_normal_spontaneous_dialogue_and_reactions_remain_allowed(self):
        body = ("Рината — Эд?\n"
                "Рина вздрогнула, выругалась и отвела глаза.")
        self.assertEqual(detect_pov_decisions(body), [])

    def test_existing_ongoing_hug_can_continue_but_npc_hug_is_not_her_consent(self):
        quote = "Она коснулась его настоящей тёплой руки и обняла Эдриана."
        entry = [{
            "scene_quote": quote,
            "source": "continued",
            "source_quote": "Рината обняла Эдриана",
            "reason": "В предыдущем сохранённом эпизоде Рината сама начала это объятие.",
        }]
        issues, found = validate_pov_provenance(
            entry, self.body, "(Посмотреть на него)",
            ["Рината обняла Эдриана и не отпустила."],
        )
        self.assertEqual(found, 1)
        self.assertEqual(issues, [])
        issues, _ = validate_pov_provenance(
            [{**entry[0], "source_quote": "Эдриан обнял Ринату"}],
            self.body, "(Посмотреть на него)",
            ["Эдриан обнял Ринату."],
        )
        self.assertTrue(issues)

    def test_negated_embrace_and_ordinary_admission_are_not_new_choices(self):
        self.assertEqual(
            detect_pov_decisions(
                "Рина не обняла Эдриана. Она хотела обнять его, но не решилась."
            ), [],
        )
        self.assertEqual(
            detect_pov_decisions("Рината призналась, что замёрзла, и улыбнулась."), [],
        )
        self.assertEqual(
            [entry["category"] for entry in detect_pov_decisions(
                "Рината призналась ему в любви."
            )], ["commitment"],
        )

    def test_new_kiss_and_promise_need_separate_permissions(self):
        body = "Рина поцеловала Эдриана. Она пообещала ему остаться навсегда."
        found = detect_pov_decisions(body)
        self.assertEqual([x["category"] for x in found], ["kiss", "commitment"])
        errors, count = validate_pov_provenance([], body, "(Оглядеться)")
        self.assertEqual(count, 2)
        self.assertTrue(any("поцеловала" in issue for issue in errors))
        self.assertTrue(any("пообещала" in issue for issue in errors))


if __name__ == "__main__":
    unittest.main()
