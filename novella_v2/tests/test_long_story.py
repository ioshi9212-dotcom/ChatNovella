"""120 consecutive narrative turns with cast changes, selective memory and audits."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novella_v2.engine import Engine, empty_actor


def physical(position):
    return {
        "position": position, "clothing": "чёрная рубашка",
        "footwear": "ботинки", "items": [], "physical": "",
        "unfinished": [],
    }


class TestLongStory(unittest.TestCase):
    def test_120_turns_continuity_and_rollback(self):
        with TemporaryDirectory() as temp:
            basedir = Path(temp)
            instr = basedir / "instructions"
            instr.mkdir()
            (instr / "rules.md").write_text("Механика.", encoding="utf-8")
            (instr / "skin_builder.md").write_text("Skin Builder.", encoding="utf-8")
            engine = Engine(basedir / "sessions", instr)
            original = "Канон.\nАдриан — прямой.\nСайлас — осторожный.\n"
            a, b = original.index("Адриан"), original.index("Сайлас")
            session = engine.create(
                original, [
                    {"start": 0, "end": a, "target": "novel"},
                    {"start": a, "end": b, "target": "adrian"},
                    {"start": b, "end": len(original), "target": "silas"}],
                {"adrian": "Адриан", "silas": "Сайлас"})
            sid = session["session_id"]
            engine.approve(sid, session["version"], session["canon_hash"], "Утверждаю")
            clock = datetime(2026, 9, 12, 18, 0)

            def game_state(n):
                now = clock + timedelta(minutes=n)
                present = {"adrian": physical("у окна")}
                if 31 <= n <= 50:
                    present["silas"] = physical("рядом с дверью")
                if n >= 61:
                    present["marcus"] = physical("за столом")
                return {
                    "turn": n, "datetime": now.isoformat(),
                    "location": "Квартира" if n <= 60 else "Кафе",
                    "pov": physical("за столом"), "present": present,
                    "remote": {}, "objects": ["письмо в ящике"], "unfinished": [],
                }

            engine.set_opening(sid, engine.status(sid)["version"], game_state(0))
            all_events = 0
            all_knowledge = {"adrian": 0, "silas": 0, "marcus": 0}
            checks = 0
            for n in range(1, 121):
                proposals = [{"name": "marcus", "display": "Маркус",
                              "established_text": "Маркус встретил компанию в кафе."}] if n == 61 else None
                reader = "Ответ героини #" + str(n)
                scene = ("**Адриан** — Сообщение " + str(n) +
                         ".\n**POV** — Поняла.\n")
                people = ["adrian"]
                if 31 <= n <= 50:
                    people.append("silas")
                    scene += "**Сайлас** — Присутствую.\n"
                if n >= 61:
                    people.append("marcus")
                    scene += "**Маркус** — Я здесь.\n"
                if proposals:
                    scene += proposals[0]["established_text"]
                engine.director_brief(sid)
                prep = engine.prepare(sid, reader, people, new_characters=proposals)
                parts = []
                for j in range(prep["chunk_count"]):
                    parts.append(engine.read_chunk(sid, prep["packet_id"], j)["content"])
                context = json.loads("".join(parts))
                self.assertIn("characters/adrian.md", context["files"])
                if n < 31 or n > 51:
                    self.assertNotIn("characters/silas.md", context["files"])
                if n >= 62:
                    self.assertIn("characters/marcus.md", context["files"])
                self.assertEqual(context["reader_text"], reader)
                entries = []
                if n % 10 == 0:
                    entries.append({"owner": "adrian",
                                    "fact": "Получил сообщение " + str(n),
                                    "source": "Сообщение " + str(n)})
                    all_knowledge["adrian"] += 1
                if n in (35, 45):
                    entries.append({"owner": "silas", "fact": "Обсуждал сообщение " + str(n),
                                    "source": "Присутствую."})
                    all_knowledge["silas"] += 1
                if n in (65, 85, 105):
                    entries.append({"owner": "marcus", "fact": "Обсуждал сообщение " + str(n),
                                    "source": "Я здесь."})
                    all_knowledge["marcus"] += 1
                events = []
                if n % 12 == 0:
                    events.append({
                        "text": "Обсудили сюжетную веху №" + str(n),
                        "participants": ["pov", "adrian"]})
                    all_events += 1
                relations = []
                if n == 20:
                    relations.append({"source": "adrian", "target": "pov",
                                      "status": "acquainted", "reason": "Встретились"})
                    relations.append({"source": "adrian", "target": "pov",
                                      "metric": "доверие", "value": 35,
                                      "reason": "Поговорили"})
                if n == 45:
                    relations.append({"source": "silas", "target": "pov",
                                      "status": "acquainted", "reason": "Встретились"})
                    relations.append({"source": "silas", "target": "pov",
                                      "metric": "симпатия", "value": 12,
                                      "reason": "Встреча"})
                if n == 65:
                    relations.append({"source": "marcus", "target": "pov",
                                      "status": "acquainted", "reason": "Знакомство"})
                    relations.append({"source": "marcus", "target": "pov",
                                      "metric": "интерес", "value": 15,
                                      "reason": "Знакомство"})
                request_id = "narrative-" + str(n)
                payload = {
                    "request_id": request_id, "expected_version": prep["version"],
                    "packet_id": prep["packet_id"],
                    "chunk_hashes": prep["chunk_hashes"],
                    "reader_text": reader, "narration": scene,
                    "participants": people,
                    "state": game_state(n), "knowledge": entries,
                    "events": events, "relations": relations,
                }
                saved = engine.commit(sid, payload)
                self.assertTrue(saved["saved"])
                self.assertEqual(saved["turn"], n)
                if n == 91:
                    engine.rollback(sid, saved["version"], request_id, "Исправление сцены")
                    engine.director_brief(sid)
                    fixed = engine.prepare(sid, reader, people)
                    for j in range(fixed["chunk_count"]):
                        engine.read_chunk(sid, fixed["packet_id"], j)
                    payload.update({
                        "request_id": "narrative-91-replacement",
                        "expected_version": fixed["version"],
                        "packet_id": fixed["packet_id"],
                        "chunk_hashes": fixed["chunk_hashes"],
                        "narration": scene + "**Адриан** — Исправленная реплика.\n",
                    })
                    saved = engine.commit(sid, payload)
                    self.assertEqual(saved["turn"], 91)
                if n % 15 == 0:
                    self.assertEqual(saved["audit_due"], 60 if n % 60 == 0 else 15)
                    audit = engine.prepare(sid, "", [], kind="audit")
                    collected = []
                    for j in range(audit["chunk_count"]):
                        collected.append(engine.read_chunk(sid, audit["packet_id"], j)["content"])
                    audit_data = json.loads("".join(collected))
                    self.assertEqual(len(audit_data["recent_scenes"]), 15)
                    if n % 60 == 0:
                        self.assertIn("knowledge/silas.md", audit_data["files"])
                        if n == 120:
                            self.assertIn("knowledge/marcus.md", audit_data["files"])
                    required = {"continuity", "state", "knowledge", "timeline", "relationships"}
                    if n % 60 == 0:
                        required.add("long_term")
                    engine.audit(sid, audit["version"], audit["packet_id"],
                                 audit["chunk_hashes"], {k: "Проверено" for k in required})
                    checks += 1
            self.assertEqual(engine.status(sid)["turn"], 120)
            self.assertEqual(len(engine.history(sid)), 120)
            self.assertEqual(len(engine.history(sid, query="Исправленная реплика")), 1)
            self.assertEqual(engine.status(sid)["audit_due"], 0)
            directory = basedir / "sessions" / sid
            self.assertEqual(
                (directory / "timeline.md").read_text(encoding="utf-8").count("Обсудили сюжетную веху"),
                all_events)
            for name, count in all_knowledge.items():
                self.assertEqual(
                    (directory / "knowledge" / (name + ".md")).read_text(
                        encoding="utf-8").count("Ход "),
                    count, msg=name)
            relations = json.loads((directory / "relationships.md").read_text(encoding="utf-8"))
            self.assertEqual(relations["npc_to_pov"]["adrian"]["доверие"], 35)
            self.assertEqual(relations["npc_to_pov"]["silas"]["симпатия"], 12)
            self.assertEqual(relations["npc_to_pov"]["marcus"]["интерес"], 15)
            self.assertEqual(
                json.loads((directory / "state.md").read_text(encoding="utf-8"))["turn"], 120)
            self.assertEqual(
                len((directory / "system/audit.jsonl").read_text(encoding="utf-8").splitlines()), 8)
            archive = (directory / "history.jsonl").read_text(encoding="utf-8")
            self.assertEqual(archive.count('"kind":"turn"'), 121)
            self.assertEqual(archive.count('"kind":"rollback"'), 1)
            self.assertEqual(checks, 8)


if __name__ == "__main__":
    unittest.main()
