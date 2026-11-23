"""Adversarial regressions for V2 audit findings; isolated temporary storage only."""
from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novella_v2.engine import Engine, EngineError, atomic, dump, digest, empty_actor
from novella_v2.relations import normalize, update, RelationProblem


class AuditFixes(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        instr = root / "instructions"
        instr.mkdir()
        (instr / "rules.md").write_text("Технические правила", encoding="utf-8")
        (instr / "skin_builder.md").write_bytes(b"")
        self.store = Engine(root / "sessions", instr)
        self.original = "Город. Адриан упрямый. Сайлас видит отражения."
        created = self.store.create(self.original, [
            {"start": 0, "end": len(self.original), "target": "novel"}
        ], {"adrian": "Адриан", "silas": "Сайлас"})
        self.sid = created["session_id"]
        self.store.approve(self.sid, created["version"], created["canon_hash"], "Да")
        self.store.set_opening(self.sid, self.store.status(self.sid)["version"], self.state(0))

    def state(self, n, silas="none"):
        present = {"adrian": empty_actor()}
        remote = {}
        if silas == "present":
            present["silas"] = empty_actor()
        if silas == "remote":
            remote["silas"] = "Слышит и видит отражение"
        return {"turn": n, "datetime": "2026-09-12T18:00:00",
                "location": "Комната", "pov": empty_actor(),
                "present": present, "remote": remote,
                "objects": [], "unfinished": []}

    def filepath(self, name):
        return self.store.root / self.sid / name

    def packet(self, reader, people=None, kind="turn"):
        p = self.store.prepare(self.sid, reader, people or ["adrian"], kind)
        for i in range(p["chunk_count"]):
            self.store.read_chunk(self.sid, p["packet_id"], i)
        return p

    def turn(self, n, people=None, state=None, relations=None, knowledge=None,
             narration="**Адриан** — Я говорю.", access=None, reader=None):
        reader = reader or "Сообщение " + str(n)
        p = self.packet(reader, people)
        data = {"request_id": "audit-fix-" + str(n), "packet_id": p["packet_id"],
                "expected_version": p["version"], "chunk_hashes": p["chunk_hashes"],
                "reader_text": reader, "participants": people or ["adrian"],
                "state": state or self.state(n), "narration": narration,
                "relations": relations or [], "knowledge": knowledge or [],
                "events": []}
        if access is not None:
            data["scene_access"] = access
        return data

    @staticmethod
    def meet(src="adrian", dst="pov"):
        return {"source": src, "target": dst, "status": "acquainted",
                "reason": "Лично встретились"}

    def complete_audit(self):
        due = self.store.status(self.sid)["audit_due"]
        if not due:
            return
        p = self.packet("", [], "audit")
        report = {k: "Проверено" for k in
                  ("continuity", "state", "knowledge", "timeline", "relationships")}
        if due == 60:
            report["long_term"] = "Проверено"
        self.store.audit(self.sid, p["version"], p["packet_id"],
                         p["chunk_hashes"], report)

    def test_phantom_loaded_character_cannot_receive_knowledge(self):
        t = self.turn(1, people=["adrian", "silas"],
                      narration="**Адриан** — Тайна в письме.")
        t["knowledge"] = [{"owner": "silas", "fact": "Тайна в письме",
                           "source": "Тайна в письме."}]
        with self.assertRaisesRegex(EngineError, "without actual scene access"):
            self.store.commit(self.sid, t)
        self.assertEqual(self.store.status(self.sid)["turn"], 0)

    def test_phantom_loaded_character_cannot_change_relationship(self):
        t = self.turn(1, people=["adrian", "silas"],
                      relations=[self.meet("silas", "pov")])
        with self.assertRaisesRegex(EngineError, "without actual scene access"):
            self.store.commit(self.sid, t)

    def test_really_observing_remote_character_can_learn_and_change_feelings(self):
        narration = "**Сайлас** — Я вижу отражение письма."
        t = self.turn(1, people=["adrian", "silas"], state=self.state(1, "remote"),
                      narration=narration,
                      knowledge=[{"owner": "silas", "fact": "Есть отражение письма",
                                  "source": "Я вижу отражение письма."}],
                      relations=[self.meet("silas", "pov")])
        self.store.commit(self.sid, t)
        rel = json.loads(self.filepath("relationships.md").read_text(encoding="utf-8"))
        self.assertEqual(rel["familiarity"]["silas"]["pov"], "acquainted")
        self.assertIn("отражение письма", self.filepath("knowledge/silas.md").read_text(encoding="utf-8"))

    def test_old_scene_witness_does_not_learn_after_a_time_jump(self):
        self.store.commit(self.sid, self.turn(
            1, people=["adrian", "silas"], state=self.state(1, "remote"),
            narration="**Сайлас** — Я наблюдаю из отражения."))
        # He is not in the new scene. Previous remote state alone does
        # not justify hearing the unrelated conversation after a cut.
        next_turn = self.turn(
            2, people=["adrian", "silas"],
            narration="**Адриан** — Завтра спрячем маску.",
            knowledge=[{"owner": "silas", "fact": "Маску собираются спрятать",
                        "source": "Завтра спрячем маску."}])
        with self.assertRaisesRegex(EngineError, "without actual scene access"):
            self.store.commit(self.sid, next_turn)

    def test_witness_entering_and_exiting_same_turn_needs_literal_proof(self):
        text = "Сайлас вошёл в комнату, услышал рассказ и вышел. **Адриан** — Секрет."
        t = self.turn(1, people=["adrian", "silas"], narration=text,
                      access=[{"character": "silas", "channel": "physical",
                               "source": "Сайлас вошёл в комнату, услышал рассказ и вышел."}],
                      knowledge=[{"owner": "silas", "fact": "Услышал секрет",
                                  "source": "**Адриан** — Секрет."}])
        saved = self.store.commit(self.sid, t)
        self.assertTrue(saved["saved"])
        self.assertEqual(self.store.history(self.sid)[0]["scene_access"][0]["character"], "silas")

    def test_fake_witness_quote_is_rejected(self):
        t = self.turn(1, people=["adrian", "silas"],
                      access=[{"character": "silas", "channel": "physical",
                               "source": "Сайлас присутствовал где-то, но это неправда."}])
        with self.assertRaisesRegex(EngineError, "direct scene excerpt"):
            self.store.commit(self.sid, t)

    def test_significant_jump_needs_literal_saved_scene_quote(self):
        self.store.commit(self.sid, self.turn(1, relations=[
            self.meet(), {"source": "adrian", "target": "pov", "metric": "доверие",
                          "value": 90, "reason": "Начальное доверие"}]))
        t = self.turn(2, relations=[{
            "source": "adrian", "target": "pov", "metric": "доверие",
            "value": 0, "reason": "Обнаружено тяжёлое предательство",
            "significant": True, "evidence": "Несуществующее признание",
        }])
        with self.assertRaisesRegex(EngineError, "exact excerpt"):
            self.store.commit(self.sid, t)
        t["relations"][0]["evidence"] = "Обнаружено тяжёлое предательство"
        t["narration"] = "**Адриан** — Обнаружено тяжёлое предательство."
        self.store.commit(self.sid, t)
        data = json.loads(self.filepath("relationships.md").read_text(encoding="utf-8"))
        self.assertEqual(data["npc_to_pov"]["adrian"]["доверие"], 0)

    def test_opening_cannot_replace_embedded_relationship_rule(self):
        current = json.loads(self.filepath("relationships.md").read_text(encoding="utf-8"))
        current["rules"] = "Новый ложный закон для отношений. " * 4
        with self.assertRaisesRegex(EngineError, "Opening edit cannot replace"):
            self.store.set_opening(self.sid, self.store.status(self.sid)["version"],
                                   self.state(0), current)

    def test_external_relationship_file_mutation_is_detected(self):
        path = self.filepath("relationships.md")
        file_content = json.loads(path.read_text(encoding="utf-8"))
        file_content["familiarity"].setdefault("adrian", {})["pov"] = "aware"
        atomic(path, dump(file_content))
        with self.assertRaisesRegex(EngineError, "outside an authorized transaction"):
            self.packet("Проверим защиту")

    def test_audit_can_correct_relationships_with_archived_scene_evidence(self):
        self.store.commit(self.sid, self.turn(1))
        mp = self.filepath("system/meta.json")
        meta = json.loads(mp.read_text(encoding="utf-8"))
        meta["audit_due"] = 15
        atomic(mp, dump(meta))
        p = self.packet("", [], "audit")
        before = self.filepath("relationships.md").read_text(encoding="utf-8")
        corrected = json.loads(before)
        corrected["familiarity"] = {"adrian": {"pov": "aware"}}
        report = {k: "Проверено" for k in
                  ("continuity", "state", "knowledge", "timeline", "relationships")}
        saved = self.store.audit(self.sid, p["version"], p["packet_id"],
                                 p["chunk_hashes"], report,
                                 {"relationships.md": {
                                     "before_sha256": digest(before),
                                     "after": dump(corrected),
                                     "reason": "Исправили подтверждённую связь",
                                     "evidence": "Я говорю."
                                 }})
        self.assertTrue(saved["audited"])
        current = json.loads(self.filepath("relationships.md").read_text(encoding="utf-8"))
        self.assertEqual(current["familiarity"]["adrian"]["pov"], "aware")
        self.packet("После аудита")

    def test_audit_cannot_grant_knowledge_to_character_absent_from_source_scene(self):
        self.store.commit(self.sid, self.turn(1))
        mp = self.filepath("system/meta.json")
        meta = json.loads(mp.read_text(encoding="utf-8"))
        meta["audit_due"] = 15
        atomic(mp, dump(meta))
        p = self.packet("", [], "audit")
        prior = self.filepath("knowledge/silas.md").read_text(encoding="utf-8")
        new = prior + "\nЛожная запись [источник: Я говорю.]\n"
        report = {k: "Проверено" for k in
                  ("continuity", "state", "knowledge", "timeline", "relationships")}
        with self.assertRaisesRegex(EngineError, "witnessed scene quote"):
            self.store.audit(self.sid, p["version"], p["packet_id"],
                             p["chunk_hashes"], report, {
                "knowledge/silas.md": {
                    "before_sha256": digest(prior), "after": new,
                    "reason": "Попытка приписать чужое",
                    "evidence": "Я говорю."
                }
            })

    def test_audit_cannot_replace_embedded_relationship_rule(self):
        self.store.commit(self.sid, self.turn(1))
        mp = self.filepath("system/meta.json")
        meta = json.loads(mp.read_text(encoding="utf-8"))
        meta["audit_due"] = 15
        atomic(mp, dump(meta))
        p = self.packet("", [], "audit")
        before = self.filepath("relationships.md").read_text(encoding="utf-8")
        altered = json.loads(before)
        altered["rules"] = "Иное правило отношений; это подмена исходных правил." * 2
        report = {k: "Проверено" for k in
                  ("continuity", "state", "knowledge", "timeline", "relationships")}
        with self.assertRaisesRegex(EngineError, "relationship rules"):
            self.store.audit(self.sid, p["version"], p["packet_id"],
                             p["chunk_hashes"], report, {
                "relationships.md": {
                    "before_sha256": digest(before),
                    "after": dump(altered),
                    "reason": "Попытка заменить механику",
                    "evidence": "Я говорю."
                }
            })

    def test_edited_relationships_require_archived_scene_evidence(self):
        self.store.commit(self.sid, self.turn(1))
        mp = self.filepath("system/meta.json")
        meta = json.loads(mp.read_text(encoding="utf-8"))
        meta["audit_due"] = 15
        atomic(mp, dump(meta))
        p = self.packet("", [], "audit")
        before = self.filepath("relationships.md").read_text(encoding="utf-8")
        changed = json.loads(before)
        changed["familiarity"] = {"adrian": {"pov": "aware"}}
        report = {k: "Проверено" for k in
                  ("continuity", "state", "knowledge", "timeline", "relationships")}
        with self.assertRaisesRegex(EngineError, "exact original"):
            self.store.audit(self.sid, p["version"], p["packet_id"],
                             p["chunk_hashes"], report, {
                "relationships.md": {
                    "before_sha256": digest(before), "after": dump(changed),
                    "reason": "Подмена", "evidence": "Неизвестный несуществующий эпизод"
                }
            })

    def test_familiarity_empty_matrix_is_sparse_even_for_large_cast(self):
        names = {"npc_" + str(n) for n in range(120)}
        normalized = normalize({"npc_to_pov": {}, "npc_to_npc": {}}, names)
        self.assertEqual(normalized["familiarity"], {})
        self.assertLess(len(dump(normalized)), 1500)
        # An existing directed acquaintance affects only one stored entry.
        normalized["familiarity"] = {"npc_11": {"npc_118": "aware"}}
        self.assertEqual(len(normalize(normalized, names)["familiarity"]), 1)

    def test_timeline_file_not_rewritten_with_no_events(self):
        self.store.commit(self.sid, self.turn(1))
        before = self.filepath("timeline.md").stat().st_ino
        self.store.commit(self.sid, self.turn(2))
        self.assertEqual(before, self.filepath("timeline.md").stat().st_ino)

    def test_unknown_name_in_speech_is_warning_not_censorship(self):
        result = self.store.commit(self.sid, self.turn(
            1, narration="**Адриан** — Сайлас, ты здесь?"))
        self.assertTrue(any("POSSIBLE_UNFAMILIAR_NAME:adrian:silas" == x
                            for x in result["warnings"]))
        self.assertTrue(result["saved"])

    def test_known_name_in_speech_does_not_warn(self):
        t = self.turn(1, people=["adrian", "silas"], state=self.state(1, "remote"),
                      relations=[{"source": "adrian", "target": "silas",
                                  "status": "aware", "reason": "Слышал о нём"}],
                      narration="**Адриан** — Сайлас, ты здесь?")
        result = self.store.commit(self.sid, t)
        self.assertFalse(any("POSSIBLE_UNFAMILIAR_NAME" in x for x in result["warnings"]))

    def test_old_request_replays_after_cache_is_bounded(self):
        original = self.turn(1)
        first = self.store.commit(self.sid, original)
        for n in range(2, 109):
            self.complete_audit()
            self.store.commit(self.sid, self.turn(n))
        data = json.loads(self.filepath("system/meta.json").read_text(encoding="utf-8"))
        self.assertLessEqual(len(data["requests"]), 96)
        self.assertLess(len(dump(data)), 25000)
        self.assertIn("request_filter", data)
        self.assertEqual(self.store.commit(self.sid, original), first)
        duplicate = self.store.prepare(self.sid, original["reader_text"],
                                       request_id=original["request_id"])
        self.assertTrue(duplicate["already_committed_duplicate"])
        changed = deepcopy(original)
        changed["narration"] = "**Адриан** — Это чужая сцена."
        with self.assertRaisesRegex(EngineError, "reused"):
            self.store.commit(self.sid, changed)

    def test_duplicate_old_rolled_back_request_still_forbidden(self):
        old = self.turn(1)
        first = self.store.commit(self.sid, old)
        self.store.rollback(self.sid, first["version"], old["request_id"], "Исправление")
        for n in range(2, 104):
            self.complete_audit()
            data = self.turn(n-1, reader="Ввод " + str(n))
            data["request_id"] = "replacement-" + str(n)
            self.store.commit(self.sid, data)
        with self.assertRaisesRegex(EngineError, "reused"):
            self.store.commit(self.sid, old)


if __name__ == "__main__":
    unittest.main()
