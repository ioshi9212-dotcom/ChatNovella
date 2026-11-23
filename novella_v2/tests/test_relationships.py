"""Directed relationships stay compact and canonical; no literary judgement."""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novella_v2.engine import Engine, EngineError, dump, atomic, empty_actor
from novella_v2.relations import normalize, update, RelationProblem, rule, MAX_SCALES


def physical(n, remote=False):
    return {"turn": n, "datetime": "2026-10-01T18:00:00",
            "location": "Площадь", "pov": empty_actor(),
            "present": {"adrian": empty_actor()},
            "remote": {"silas": "наблюдает через отражение"} if remote else {},
            "objects": [], "unfinished": []}


class TestRelationships(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        ins = base / "instructions"
        ins.mkdir()
        (ins / "rules.md").write_text("Технические правила", encoding="utf-8")
        (ins / "skin_builder.md").write_bytes(b"")
        self.engine = Engine(base / "sessions", ins)
        author = "Рената встречает Адриана.\nСайлас видит отражения.\n"
        split = author.index("Сайлас")
        self.initial = self.engine.create(author, [
            {"start": 0, "end": split, "target": "novel"},
            {"start": split, "end": len(author), "target": "silas"},
        ], {"adrian": "Адриан", "silas": "Сайлас"})
        self.sid = self.initial["session_id"]
        self.engine.approve(self.sid, self.initial["version"],
                            self.initial["canon_hash"], "Утверждаю")
        self.engine.set_opening(self.sid, self.engine.status(self.sid)["version"], physical(0))

    def path(self):
        return self.engine.root / self.sid / "relationships.md"

    def relation(self):
        return json.loads(self.path().read_text(encoding="utf-8"))

    def submit(self, n, changes=None, participants=None, state=None,
               reader=None, narration=None):
        people = participants if participants is not None else ["adrian"]
        message = reader or "Слова " + str(n)
        p = self.engine.prepare(self.sid, message, people)
        for i in range(p["chunk_count"]):
            self.engine.read_chunk(self.sid, p["packet_id"], i)
        return self.engine.commit(self.sid, {
            "request_id": "relation-" + str(n), "expected_version": p["version"],
            "packet_id": p["packet_id"], "chunk_hashes": p["chunk_hashes"],
            "reader_text": message,
            "narration": narration or "**Адриан** — Я говорю.",
            "participants": people, "state": state or physical(n),
            "knowledge": [], "events": [], "relations": changes or [],
        })

    @staticmethod
    def meet(src="adrian", dst="pov", status="acquainted"):
        return {"source": src, "target": dst, "status": status,
                "reason": "Непосредственное знакомство"}

    @staticmethod
    def metric(label="любовь", value=80, src="adrian",
               significant=None, reason="Значимое переживание"):
        item = {"source": src, "target": "pov",
                "metric": label, "value": value, "reason": reason}
        if significant is not None:
            item["significant"] = significant
        return item

    def test_file_starts_with_rule_and_directional_unknown_matrix(self):
        data = self.relation()
        self.assertEqual(data["rules"], rule())
        self.assertGreater(len(data["rules"]), 50)
        self.assertEqual(data["npc_to_pov"], {})
        self.assertEqual(data["npc_to_npc"], {})
        self.assertEqual(data["familiarity"]["adrian"]["silas"], "unaware")
        self.assertEqual(data["familiarity"]["silas"]["adrian"], "unaware")
        self.assertEqual(data["familiarity"]["adrian"]["pov"], "unaware")
        self.assertEqual(data["familiarity"]["pov"]["adrian"], "unaware")

    def test_full_relationship_file_in_every_turn_and_canon_packet(self):
        p = self.engine.prepare(self.sid, "Сегодня", ["adrian"])
        content = json.loads("".join(self.engine.read_chunk(
            self.sid, p["packet_id"], i)["content"]
            for i in range(p["chunk_count"])))
        self.assertEqual(json.loads(content["files"]["relationships.md"]), self.relation())
        self.assertEqual(content["files"]["relationships.md"],
                         self.path().read_text(encoding="utf-8"))

    def test_canon_initial_love_and_possession_are_not_renamed(self):
        candidate = normalize({
            "npc_to_pov": {"adrian": {"любовь": 90, "собственничество": 88,
                                      "недоверие": 36}},
            "npc_to_npc": {},
            "familiarity": {"adrian": {"pov": "acquainted"}},
        }, {"adrian", "silas"})
        self.assertEqual(set(candidate["npc_to_pov"]["adrian"]),
                         {"любовь", "собственничество", "недоверие"})
        self.assertNotIn("привязанность", candidate["npc_to_pov"]["adrian"])
        self.assertNotIn("забота", candidate["npc_to_pov"]["adrian"])
        self.assertEqual(candidate["npc_to_pov"]["adrian"]["любовь"], 90)

    def test_legacy_existing_values_are_migrated_without_guessing_the_others(self):
        old = {"npc_to_pov": {"adrian": {"любовь": 100}},
               "npc_to_npc": {"adrian": {"silas": "Считает соперником"}}}
        atomic(self.path(), dump(old))
        p = self.engine.prepare(self.sid, "Сегодня", ["adrian"])
        payload = json.loads("".join(self.engine.read_chunk(
            self.sid, p["packet_id"], i)["content"]
            for i in range(p["chunk_count"])))
        restored = json.loads(payload["files"]["relationships.md"])
        self.assertEqual(restored["npc_to_pov"], old["npc_to_pov"])
        self.assertEqual(restored["npc_to_npc"], old["npc_to_npc"])
        self.assertEqual(restored["familiarity"]["adrian"]["pov"], "acquainted")
        self.assertEqual(restored["familiarity"]["adrian"]["silas"], "acquainted")
        self.assertEqual(restored["familiarity"]["silas"]["adrian"], "unaware")
        self.submit(1)
        self.assertEqual(self.relation(), restored)

    def test_meeting_initial_score_then_small_delta_preserves_metric(self):
        self.submit(1, [self.meet(), self.metric("любовь", 89)])
        data = self.relation()
        self.assertEqual(data["npc_to_pov"]["adrian"]["любовь"], 89)
        self.submit(2, [self.metric("любовь", 92, reason="Новый опыт доверия")])
        self.assertEqual(self.relation()["npc_to_pov"]["adrian"]["любовь"], 92)

    def test_strong_jump_and_drop_allowed_only_with_explanation(self):
        self.submit(1, [self.meet(), self.metric("доверие", 100)])
        self.submit(2, [self.metric("доверие", 0, significant=True,
                                    reason="Обнаружено тяжёлое предательство")])
        self.assertEqual(self.relation()["npc_to_pov"]["adrian"]["доверие"], 0)

    def test_strong_jump_without_significance_is_refused(self):
        self.submit(1, [self.meet(), self.metric("доверие", 62)])
        with self.assertRaisesRegex(EngineError, "larger than"):
            self.submit(2, [self.metric("доверие", 40)])
        self.assertEqual(self.relation()["npc_to_pov"]["adrian"]["доверие"], 62)

    def test_russian_directed_awareness_and_independent_npc_links(self):
        changes = [self.meet("adrian", "silas", "aware")]
        self.submit(1, changes, ["adrian", "silas"], physical(1, remote=True))
        current = self.relation()
        self.assertEqual(current["familiarity"]["adrian"]["silas"], "aware")
        self.assertEqual(current["familiarity"]["silas"]["adrian"], "unaware")
        self.assertEqual(current["npc_to_npc"], {})

    def test_no_feelings_about_just_heard_of_npc(self):
        self.submit(1, [self.meet("adrian", "silas", "aware")],
                    ["adrian", "silas"], physical(1, remote=True))
        with self.assertRaisesRegex(EngineError, "not personally acquainted"):
            self.submit(2, [{
                "source": "adrian", "target": "silas",
                "metric": None, "value": "Ненавидит",
                "reason": "Предположил, не познакомившись",
            }], ["adrian", "silas"], physical(2, remote=True))

    def test_qualitative_npc_links_update_in_place_not_append(self):
        self.submit(1, [self.meet("adrian", "silas"),
                        {"source": "adrian", "target": "silas", "metric": None,
                         "value": "Опасается Сайласа", "reason": "Разговор"}],
                    ["adrian", "silas"], physical(1, remote=True))
        self.submit(2, [{"source": "adrian", "target": "silas",
                         "metric": None, "value": "Считает Сайласа опасным союзником",
                         "reason": "Появилось доверие"}],
                    ["adrian", "silas"], physical(2, remote=True))
        text = self.path().read_text(encoding="utf-8")
        self.assertIn("опасным союзником", text)
        self.assertNotIn("Опасается Сайласа", text)
        self.assertNotIn("Разговор", text)
        self.assertEqual(self.relation()["familiarity"]["silas"]["adrian"], "unaware")

    def test_pov_can_have_independent_acquaintance_status_without_numeric_scales(self):
        self.submit(1, [self.meet("pov", "adrian", "aware")])
        current = self.relation()
        self.assertEqual(current["familiarity"]["pov"]["adrian"], "aware")
        self.assertEqual(current["familiarity"]["adrian"]["pov"], "unaware")
        self.assertEqual(current["npc_to_pov"], {})

    def test_no_changes_file_is_byte_identical_and_not_replaced(self):
        before = self.path().read_bytes()
        inode = self.path().stat().st_ino
        self.submit(1, [])
        self.assertEqual(self.path().read_bytes(), before)
        self.assertEqual(self.path().stat().st_ino, inode)

    def test_repeating_same_metric_is_not_log_spam(self):
        self.submit(1, [self.meet(), self.metric("любовь", 54)])
        with self.assertRaisesRegex(EngineError, "Unchanged metric"):
            self.submit(2, [self.metric("любовь", 54)])

    def test_max_ten_individual_scales_not_ten_generic_fixed_labels(self):
        scales = [self.metric("авторская_шкала_" + str(n), n + 10) for n in range(MAX_SCALES)]
        self.submit(1, [self.meet()] + scales)
        self.assertEqual(len(self.relation()["npc_to_pov"]["adrian"]), 10)
        with self.assertRaisesRegex(EngineError, "exceed 10"):
            self.submit(2, [self.metric("одиннадцатая", 45)])

    def test_link_description_rejects_runaway_memo(self):
        changes = [self.meet("adrian", "silas"),
                   {"source": "adrian", "target": "silas",
                    "metric": None, "value": "слова " * 70, "reason": "Контекст"}]
        with self.assertRaisesRegex(EngineError, "must be short"):
            self.submit(1, changes, ["adrian", "silas"], physical(1, remote=True))

    def test_npc_cannot_update_nonparticipating_npc(self):
        with self.assertRaisesRegex(EngineError, "Only participants"):
            self.submit(1, [self.meet("adrian", "silas")])

    def test_remote_observer_can_update_toward_pov(self):
        self.submit(1, [self.meet("silas", "pov"),
                        self.metric("интерес", 70, src="silas")],
                    ["adrian", "silas"], physical(1, remote=True))
        self.assertEqual(self.relation()["npc_to_pov"]["silas"]["интерес"], 70)

    def test_downgraded_acquaintance_clears_personal_feelings(self):
        self.submit(1, [self.meet(), self.metric("любовь", 80)])
        self.submit(2, [{"source": "adrian", "target": "pov",
                         "status": "unaware",
                         "reason": "После потери памяти перестал узнавать"}])
        self.assertEqual(self.relation()["npc_to_pov"], {})
        self.assertEqual(self.relation()["familiarity"]["adrian"]["pov"], "unaware")

    def test_rollback_restores_prior_status_and_numbers(self):
        first = self.submit(1, [self.meet(), self.metric("любовь", 50)])
        second = self.submit(2, [self.metric("любовь", 53)])
        self.engine.rollback(self.sid, second["version"], "relation-2", "Перепроверка")
        self.assertEqual(self.relation()["npc_to_pov"]["adrian"]["любовь"], 50)
        self.assertEqual(self.relation()["familiarity"]["adrian"]["pov"], "acquainted")

    def test_new_character_expands_matrix_but_does_not_invent_connections(self):
        old = self.relation()
        self.engine.add_character(self.sid, self.engine.status(self.sid)["version"],
                                  "julian", "Джулиан",
                                  "Джулиан появляется в городе.")
        data = self.relation()
        self.assertEqual(data["familiarity"]["adrian"]["julian"], "unaware")
        self.assertEqual(data["familiarity"]["julian"]["adrian"], "unaware")
        self.assertEqual(data["familiarity"]["julian"]["pov"], "unaware")
        self.assertEqual(data["familiarity"]["pov"]["julian"], "unaware")
        self.assertEqual(data["npc_to_pov"], old["npc_to_pov"])

    def test_invalid_status_and_unknown_with_metrics_fail(self):
        bad = {"familiarity": {"adrian": {"pov": "unaware"}},
               "npc_to_pov": {"adrian": {"любовь": 75}}, "npc_to_npc": {}}
        with self.assertRaisesRegex(RelationProblem, "Unacquainted"):
            normalize(bad, {"adrian", "silas"})
        with self.assertRaisesRegex(RelationProblem, "Unknown familiarity status"):
            normalize({"familiarity": {"adrian": {"pov": "married"}},
                       "npc_to_pov": {}, "npc_to_npc": {}}, {"adrian"})

    def test_scene_log_records_reason_without_adding_it_to_relationship_file(self):
        self.submit(1, [self.meet(), self.metric("любовь", 89,
                     reason="Влюблён после близкого знакомства")])
        raw = self.path().read_text(encoding="utf-8")
        self.assertNotIn("Влюблён после близкого знакомства", raw)
        turn = self.engine.history(self.sid)[0]
        self.assertEqual(turn["relations"][1]["reason"],
                         "Влюблён после близкого знакомства")


if __name__ == "__main__":
    unittest.main()
