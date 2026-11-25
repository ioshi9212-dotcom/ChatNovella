"""Plot-memory V2: durable hooks, director preselect, safe backfill, audit."""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novella_v2.engine import Engine, EngineError, empty_actor
from novella_v2 import hooks as story_hooks


CANON = ("Мир. Натан действует по неизвестному заказу.\n"
         "Селин должна появиться между 90 и 120 ходами.\n"
         "Адриан: хранит старое письмо.\n"
         "Селин: независимая героиня.\n")
CLIENT = "Натан действует по неизвестному заказу."
APPEARANCE = "Селин должна появиться между 90 и 120 ходами."
PROGRESS = "Натан признал, что заказчик приходил лично, но имени не назвал."
HISTORY_QUOTE = "Джулиан сказал, что будет следить за Натаном."


def hook(id, title, kind, source, *, arc_id="rabbits",
         status="open", detail=None, participants=None,
         window_start=None, window_end=None, trigger=None):
    return {
        "id": id, "arc_id": arc_id, "title": title, "kind": kind,
        "status": status, "detail": detail or title,
        "participants": participants if participants is not None else [],
        "source": source, "window_start": window_start,
        "window_end": window_end, "trigger": trigger,
    }


def state(turn):
    return {"turn": turn, "datetime": "2026-05-05T19:00:00",
            "location": "Комната", "pov": empty_actor(),
            "present": {"adrian": empty_actor()}, "remote": {},
            "objects": [], "unfinished": []}


class TestStoryHooks(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        instructions = root / "instructions"
        instructions.mkdir()
        (instructions / "rules.md").write_text("Тест", encoding="utf-8")
        (instructions / "skin_builder.md").write_text("", encoding="utf-8")
        self.engine = Engine(root / "sessions", instructions)
        self.original = CANON

    def make(self, initial=True):
        pos1, pos2 = CANON.index("Адриан:"), CANON.index("Селин:")
        initial_hooks = [
            hook("nathan_client", "Неизвестный заказчик Натана",
                 "mystery", CLIENT, participants=["adrian"]),
            hook("seline_arrival", "Появление Селин", "appearance",
                 APPEARANCE, participants=["seline"],
                 window_start=90, window_end=120),
        ] if initial else None
        created = self.engine.create(
            CANON, [{"start": 0, "end": pos1, "target": "novel"},
                    {"start": pos1, "end": pos2, "target": "adrian"},
                    {"start": pos2, "end": len(CANON), "target": "seline"}],
            {"adrian": "Адриан", "seline": "Селин"},
            initial_threads=[{"id": "rabbits", "title": "Кроличья нора",
                              "status": "active", "note": "Идёт следствие",
                              "source": CLIENT}],
            initial_hooks=initial_hooks)
        sid = created["session_id"]
        self.engine.approve(sid, created["version"], created["canon_hash"],
                            "Утверждаю синтетический канон")
        current = self.engine.status(sid)
        self.engine.set_opening(sid, current["version"], state(0))
        return sid

    def packet(self, sid, number):
        p = self.engine.prepare(
            sid, str(number), ["adrian"], "turn", f"hook-turn-{number}")
        for i in p["unread_chunk_indices"]:
            self.engine.read_chunk(sid, p["packet_id"], i)
        return p

    def turn(self, sid, number, narration="Рината осталась в комнате.",
             updates=None):
        p = self.packet(sid, number)
        return self.engine.commit(sid, {
            "request_id": f"hook-turn-{number}", "expected_version": p["version"],
            "packet_id": p["packet_id"], "chunk_hashes": p["chunk_hashes"],
            "reader_text": str(number), "narration": narration,
            "participants": ["adrian"], "state": state(number),
            "knowledge": [], "events": [], "relations": [],
            "hook_updates": updates or [],
        })

    def saved_hooks(self, sid):
        path = self.engine.root / sid / "hooks.md"
        return story_hooks.parse(path.read_text(encoding="utf-8"),
                                 {"adrian", "seline"})

    def test_seed_and_director_brief_before_participant_selection(self):
        sid = self.make()
        brief = self.engine.director_brief(sid)
        self.assertEqual(brief["turn"], 0)
        self.assertEqual({x["id"] for x in brief["unresolved_hooks"]},
                         {"nathan_client", "seline_arrival"})
        self.assertIn({"id": "seline", "display": "Селин"}, brief["cast"])
        self.assertEqual(len(brief["open_arcs"]), 1)
        self.assertEqual(len(brief["author_window_candidates"]), 1)
        self.assertEqual(brief["author_window_candidates"][0]["window_end"], 120)
        self.assertTrue(brief["author_window_candidates"][0]["tracked"])
        self.assertEqual(brief["author_deadlines_overdue"], [])
        self.assertEqual(
            next(x for x in brief["unresolved_hooks"]
                 if x["id"] == "seline_arrival")["deadline_state"],
            "author_window_future")
        # A preselect brief doesn't implicitly load Seline's private card.
        self.assertNotIn("characters/seline.md", json.dumps(brief, ensure_ascii=False))

    def test_each_hook_survives_other_hook_progress_and_rollback(self):
        sid = self.make()
        old = (self.engine.root / sid / "hooks.md").read_bytes()
        self.turn(sid, 1, PROGRESS, updates=[
            hook("nathan_client", "Неизвестный заказчик Натана",
                 "mystery", PROGRESS, status="active",
                 detail="Натан подтвердил визит заказчика, имя неизвестно",
                 participants=["adrian"])])
        rows = {x["id"]: x for x in self.saved_hooks(sid)}
        self.assertEqual(set(rows), {"nathan_client", "seline_arrival"})
        self.assertEqual(rows["nathan_client"]["last_turn"], 1)
        self.assertEqual(rows["seline_arrival"]["last_turn"], 0)
        current = self.engine.status(sid)
        self.engine.rollback(sid, current["version"], "hook-turn-1", "Тестовый откат")
        self.assertEqual((self.engine.root / sid / "hooks.md").read_bytes(), old)

    def test_legacy_missing_hook_file_keeps_playable_and_can_be_backfilled(self):
        sid = self.make(initial=False)
        ledger = self.engine.root / sid / "hooks.md"
        ledger.unlink()
        self.assertTrue(self.engine.director_brief(sid)["empty_ledger"])
        self.assertEqual(len(self.engine.director_brief(sid)["untracked_author_windows"]), 1)
        p = self.packet(sid, 1)
        packet = json.loads((self.engine.root / sid / "system/packet.json").read_text())
        self.assertEqual(packet["files"]["hooks.md"], story_hooks.HEADER)
        self.turn(sid, 1, HISTORY_QUOTE)
        self.turn(sid, 2)
        version = self.engine.status(sid)["version"]
        result = self.engine.backfill_hooks(sid, version, [
            {**hook("nathan_client", "Неизвестный заказчик Натана",
                    "mystery", CLIENT), "origin_turn": 0},
            {**hook("julian_watch", "Обещание Джулиана", "commitment",
                    HISTORY_QUOTE, detail="Следить за Натаном"),
             "origin_turn": 1},
        ])
        self.assertEqual(result["added"], 2)
        self.assertEqual(self.engine.status(sid)["turn"], 2)
        self.assertEqual(len(self.engine.history(sid)), 2)
        log = (self.engine.root / sid / "system/audit.jsonl").read_text()
        self.assertIn('"kind":"hook_backfill"', log)
        self.assertEqual(len(self.saved_hooks(sid)), 2)
        self.assertEqual(
            {h["id"] for h in self.engine.director_brief(sid)["unresolved_hooks"]},
            {"nathan_client", "julian_watch"})
        with self.assertRaises(story_hooks.HookProblem):
            self.engine.backfill_hooks(sid, result["version"], [
                {**hook("nathan_client", "Попытка переписать", "mystery", CLIENT),
                 "origin_turn": 0},
            ])

    def test_unsupported_deadline_or_invented_source_rejected(self):
        bad = hook("seline_arrival", "Селин", "appearance", APPEARANCE,
                   window_start=90, window_end=130)
        with self.assertRaises(story_hooks.HookProblem):
            story_hooks.seed([bad], CANON, {"seline"})
        sid = self.make(initial=False)
        current = self.engine.status(sid)
        with self.assertRaises(story_hooks.HookProblem):
            self.engine.backfill_hooks(sid, current["version"], [
                {**hook("fake", "Выдуманная зацепка", "mystery",
                         "Несуществующее раскрытие замысла Натана."),
                 "origin_turn": 0},
            ])
        self.assertEqual(self.engine.status(sid)["version"], current["version"])

    def test_closed_hook_does_not_erase_sibling(self):
        sid = self.make()
        self.turn(sid, 1, PROGRESS, updates=[
            hook("nathan_client", "Неизвестный заказчик Натана",
                 "mystery", PROGRESS, status="closed",
                 detail="Натан подтвердил имя",
                 participants=["adrian"])])
        self.assertEqual([h["id"] for h in self.saved_hooks(sid)],
                         ["seline_arrival"])
        self.assertEqual(self.engine.history(sid)[-1]["hook_updates"][0]["status"],
                         "closed")

    def test_cannot_close_arc_while_sibling_hooks_remain(self):
        sid = self.make()
        p = self.packet(sid, 1)
        data = {
            "request_id": "hook-turn-1", "expected_version": p["version"],
            "packet_id": p["packet_id"], "chunk_hashes": p["chunk_hashes"],
            "reader_text": "1", "narration": PROGRESS,
            "participants": ["adrian"], "state": state(1),
            "knowledge": [], "events": [], "relations": [],
            "thread_updates": [{
                "id": "rabbits", "title": "Кроличья нора",
                "status": "closed", "note": "Завершено",
                "source": PROGRESS,
            }],
        }
        with self.assertRaisesRegex(EngineError, "An arc cannot close"):
            self.engine.commit(sid, data)
        self.assertEqual(self.engine.status(sid)["turn"], 0)

    def test_repeated_unchanged_hooks_cannot_hide_staleness(self):
        source = story_hooks.seed([
            hook("nathan_client", "Неизвестный заказчик Натана",
                 "mystery", CLIENT, participants=["adrian"])
        ], CANON, {"adrian"})
        change = hook("nathan_client", "Неизвестный заказчик Натана",
                      "mystery", PROGRESS, status="active",
                      detail="Натан знает заказчика, имени не назвал",
                      participants=["adrian"])
        progressed, _ = story_hooks.apply(source, [change], PROGRESS, 1,
                                          {"adrian"})
        with self.assertRaisesRegex(story_hooks.HookProblem, "real status"):
            story_hooks.apply(progressed, [change], PROGRESS, 2, {"adrian"})

    def test_author_deadlines_are_diagnostic_not_forced_plot_events(self):
        row = story_hooks.seed(
            [hook("seline_arrival", "Селин", "appearance", APPEARANCE,
                  window_start=90, window_end=120)], CANON, {"seline"})
        hook_rows = story_hooks.parse(row, {"seline"})
        overview = story_hooks.director_brief(hook_rows, 153)
        self.assertEqual(overview["author_deadlines_overdue"][0]["id"],
                         "seline_arrival")
        self.assertEqual(overview["unresolved_hooks"][0]["last_turn"], 0)
        hints = story_hooks.author_window_candidates(CANON, 153)
        self.assertEqual(hints[0]["window_start"], 90)
        self.assertTrue(hints[0]["overdue"])

    def test_long_author_line_preserves_multiple_deadlines(self):
        original = ("Обстоятельства. " * 55 +
                    "Кэллум появляется между 35-м и 50-м ходом. "
                    "Селин появляется между 90-м и 120-м ходом. "
                    "Остальные персонажи должны появиться до 100-го хода.")
        hints = story_hooks.author_window_candidates(original, 153)
        self.assertEqual({h["window_end"] for h in hints}, {50, 100, 120})
        self.assertTrue(all(str(h["window_end"]) in h["source"] for h in hints))
        self.assertTrue(all(h["overdue"] for h in hints))

    def test_audit_requires_every_hook_and_arc_and_checks_provenance(self):
        sid = self.make()
        for number in range(1, 16):
            self.turn(sid, number)
        p = self.engine.prepare(sid, "", [], "audit", "story-audit")
        for i in p["unread_chunk_indices"]:
            self.engine.read_chunk(sid, p["packet_id"], i)
        report = {key: "Сверено" for key in
                  ("continuity", "state", "knowledge", "timeline", "relationships")}
        with self.assertRaisesRegex(EngineError, "Audit must check required domains"):
            self.engine.audit(sid, p["version"], p["packet_id"],
                              p["chunk_hashes"], report)
        report["story"] = {
            "hook:nathan_client": "Натан остаётся действующим неизвестным заказчиком.",
            "hook:seline_arrival": "Появление Селин ожидается в авторском диапазоне.",
            "arc:rabbits": "Арка криминальной линии активна, не закрыта.",
        }
        result = self.engine.audit(sid, p["version"], p["packet_id"],
                                   p["chunk_hashes"], report)
        self.assertTrue(result["audited"])
        self.assertEqual(result["plot_findings"]["open_hook_count"], 2)
        self.assertEqual(result["plot_findings"]["open_arc_count"], 1)


if __name__ == "__main__":
    unittest.main()
