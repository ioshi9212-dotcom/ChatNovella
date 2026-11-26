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


def fixture_plan(brief):
    return {"focus_ids": [], "direction": "Тестовая сцена сохраняет непрерывность.",
            "world": "В этом тестовом эпизоде нет закадровых изменений.",
            "deferred": "Открытые линии сохраняются до подходящего события.",
            "priority": {i: "Проверено; ждём подходящего сюжетного развития."
                         for i in brief["plan_priority_ids"]}}


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
        brief = self.engine.director_brief(sid)
        p = self.engine.prepare(
            sid, str(number), ["adrian"], "turn", f"hook-turn-{number}",
            story_plan=fixture_plan(brief))
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
        resolved = "Натан назвал заказчика: им оказался Марк."
        self.turn(sid, 1, resolved, updates=[
            hook("nathan_client", "Неизвестный заказчик Натана",
                 "mystery", resolved, status="closed",
                 detail="Заказчиком Натана оказался Марк",
                 participants=["adrian"])])
        self.assertEqual({h["id"]: h["status"] for h in self.saved_hooks(sid)},
                         {"nathan_client": "closed", "seline_arrival": "open"})
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
                  window_start=90, window_end=120, participants=["seline"])], CANON, {"seline"})
        hook_rows = story_hooks.parse(row, {"seline"})
        overview = story_hooks.director_brief(hook_rows, 153)
        self.assertEqual(overview["author_deadlines_overdue"][0],
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

    def test_director_receipt_expires_at_next_turn(self):
        sid = self.make()
        with self.assertRaisesRegex(EngineError, "Read director brief"):
            self.engine.prepare(sid, "1", ["adrian"], request_id="hook-turn-1")
        self.turn(sid, 1)
        with self.assertRaisesRegex(EngineError, "Read director brief"):
            self.engine.prepare(sid, "2", ["adrian"], request_id="hook-turn-2")

    def test_backfill_survives_last_scene_rollback_and_keeps_last_development(self):
        sid = self.make(initial=False)
        self.turn(sid, 1, HISTORY_QUOTE)
        self.turn(sid, 2, PROGRESS)
        self.turn(sid, 3)
        version = self.engine.status(sid)["version"]
        result = self.engine.backfill_hooks(sid, version, [
            {**hook("nathan_client", "Заказчик Натана", "mystery", CLIENT,
                    detail="Имя неизвестно, заказчик приходил лично"),
             "origin_turn": 0, "last_turn": 2, "last_evidence": PROGRESS}
        ])
        self.engine.rollback(sid, result["version"], "hook-turn-3", "Отмена")
        hooks = self.saved_hooks(sid)
        self.assertEqual(hooks[0]["origin_turn"], 0)
        self.assertEqual(hooks[0]["last_turn"], 2)
        self.assertEqual(hooks[0]["last_evidence"], PROGRESS)

    def test_appearance_completion_retained_with_actual_turn(self):
        row = story_hooks.seed([
            hook("seline_arrival", "Селин", "appearance", APPEARANCE,
                 window_start=90, window_end=120, participants=["seline"])
        ], CANON, {"seline"})
        quote = "Селин вошла и представилась."
        row, _ = story_hooks.apply(row, [
            hook("seline_arrival", "Селин", "appearance", quote,
                 status="closed", detail="Селин вошла в историю и представилась",
                 participants=["seline"])
        ], quote, 100, {"seline"})
        brief = story_hooks.director_brief(story_hooks.parse(row), 153)
        self.assertEqual(brief["author_deadlines_overdue"], [])
        self.assertEqual(brief["resolved_hooks"][0]["deadline_state"], "fulfilled")
        self.assertEqual(brief["resolved_hooks"][0]["last_turn"], 100)

    def test_ledger_deletion_is_detected_even_with_valid_empty_header(self):
        sid = self.make()
        (self.engine.root / sid / "hooks.md").write_text(
            story_hooks.HEADER, encoding="utf-8")
        with self.assertRaisesRegex(EngineError, "Story memory changed"):
            self.engine.director_brief(sid)

    def test_explicit_reopening_keeps_original_evidence(self):
        sid = self.make()
        quote = "Натан назвал заказчика: им оказался Марк."
        self.turn(sid, 1, quote, [
            hook("nathan_client", "Неизвестный заказчик Натана", "mystery",
                 quote, status="closed", detail="Заказчиком назван Марк",
                 participants=["adrian"])
        ])
        self.turn(sid, 2, PROGRESS)
        self.turn(sid, 3)
        version = self.engine.status(sid)["version"]
        self.engine.backfill_hooks(sid, version, [
            {**hook("nathan_client", "Неизвестный заказчик Натана", "mystery",
                     CLIENT, status="active", detail="Натан не раскрыл имя",
                     participants=["adrian"]),
             "origin_turn": 0, "last_turn": 2, "last_evidence": PROGRESS,
             "reopen": True}
        ])
        rows = {h["id"]: h for h in self.saved_hooks(sid)}
        self.assertEqual(rows["nathan_client"]["status"], "active")
        self.assertEqual(rows["nathan_client"]["origin"], CLIENT)

    def test_historical_closure_reconciles_open_appearance_without_losing_identity(self):
        row = story_hooks.seed([
            hook("seline_arrival", "Селин", "appearance", APPEARANCE,
                 participants=["seline"], window_start=90, window_end=120)
        ], CANON, {"seline"})
        quote = "Селин вошла и представилась."
        closed = story_hooks.backfill(row, [{
            **hook("seline_arrival", "Селин", "appearance", APPEARANCE,
                   participants=["seline"], window_start=90, window_end=120,
                   status="closed", detail="Селин появилась на сотом ходу"),
            "origin_turn": 0, "last_turn": 100, "last_evidence": quote,
            "reconcile": True
        }], CANON, {100: quote}, 153, {"seline"})
        item = story_hooks.parse(closed)[0]
        self.assertEqual(item["last_turn"], 100)
        self.assertEqual(item["origin"], APPEARANCE)
        self.assertEqual(story_hooks.director_brief([item], 153)["resolved_hooks"][0]
                         ["deadline_state"], "fulfilled")

    def test_future_and_blocked_hooks_are_not_stale(self):
        original = CANON + "Фотографии Ханы будут сделаны 10 мая.\n"
        ledger = story_hooks.seed([
            hook("hana_photo", "Фотографии Ханы", "commitment",
                 "Фотографии Ханы будут сделаны 10 мая.", status="blocked",
                 trigger="date:2026-05-10"),
            hook("seline_arrival", "Селин", "appearance", APPEARANCE,
                 participants=["seline"], window_start=90, window_end=120),
        ], original, {"seline"})
        brief = story_hooks.director_brief(story_hooks.parse(ledger), 50,
                                          world_datetime="2026-05-05T19:00:00")
        self.assertEqual(brief["stale_hooks"], [])
        self.assertEqual(next(h for h in brief["unresolved_hooks"]
                              if h["id"] == "hana_photo")["deadline_state"],
                         "awaiting_condition")

    def test_untracked_canon_requires_own_audit_report(self):
        sid = self.make(initial=False)
        for n in range(1, 16):
            self.turn(sid, n)
        p = self.engine.prepare(sid, "", [], "audit", "plot-window-audit")
        for i in p["unread_chunk_indices"]:
            self.engine.read_chunk(sid, p["packet_id"], i)
        report = {key: "Сверено" for key in
                  ("continuity", "state", "knowledge", "timeline", "relationships")}
        report["story"] = {"arc:rabbits": "Арка остаётся активной, не закрыта."}
        with self.assertRaisesRegex(EngineError, "Plot audit must address"):
            self.engine.audit(sid, p["version"], p["packet_id"], p["chunk_hashes"], report)
        for w in story_hooks.author_window_candidates(CANON, 15):
            report["story"][w["id"]] = "Срок Селин ещё не наступил, он проверен по канону."
        result = self.engine.audit(sid, p["version"], p["packet_id"], p["chunk_hashes"], report)
        self.assertTrue(result["audited"])

    def test_story_plan_required_and_bound_to_packet(self):
        sid = self.make()
        brief = self.engine.director_brief(sid)
        with self.assertRaisesRegex(story_hooks.HookProblem, "STORY_PLAN_REQUIRED"):
            self.engine.prepare(sid, "1", ["adrian"], request_id="hook-turn-1")
        plan = fixture_plan(brief)
        plan["focus_ids"] = ["hook:missing"]
        with self.assertRaisesRegex(story_hooks.HookProblem, "known IDs"):
            self.engine.prepare(sid, "1", ["adrian"], request_id="hook-turn-1",
                                story_plan=plan)
        plan["focus_ids"] = ["hook:nathan_client"]
        p = self.engine.prepare(sid, "1", ["adrian"], request_id="hook-turn-1",
                                story_plan=plan)
        replay = self.engine.prepare(sid, "1", ["adrian"], request_id="hook-turn-1")
        self.assertEqual(p["packet_id"], replay["packet_id"])
        packet = json.loads((self.engine.root / sid / "system/packet.json").read_text())
        self.assertEqual(packet["story_plan"], plan)
        self.assertEqual(packet["story_health"]["overdue_hook_ids"], [])

    def test_closed_result_stays_in_next_writing_context(self):
        sid = self.make()
        quote = "Натан назвал заказчика: им оказался Марк."
        self.turn(sid, 1, quote, [
            hook("nathan_client", "Неизвестный заказчик Натана", "mystery",
                 quote, status="closed", detail="Заказчиком назван Марк",
                 participants=["adrian"])
        ])
        brief = self.engine.director_brief(sid)
        self.assertEqual(brief["resolved_hooks"][0]["detail"], "Заказчиком назван Марк")
        self.packet(sid, 2)
        packet = json.loads((self.engine.root / sid / "system/packet.json").read_text())
        self.assertEqual(packet["resolved_outcomes"][0]["detail"], "Заказчиком назван Марк")
        self.assertNotIn("nathan_client", packet["files"]["hooks.md"])
        self.assertIn("seline_arrival", packet["files"]["hooks.md"])

    def test_orphan_hook_rejected_and_historical_arc_repair_survives_rollback(self):
        sid = self.make(initial=False)
        self.turn(sid, 1, HISTORY_QUOTE)
        self.turn(sid, 2)
        version = self.engine.status(sid)["version"]
        addition = {**hook("julian_watch", "Обещание Джулиана", "commitment",
                            HISTORY_QUOTE, arc_id="julian_arc"),
                    "origin_turn": 1}
        with self.assertRaisesRegex(story_hooks.HookProblem, "existing open arc"):
            self.engine.backfill_hooks(sid, version, [addition])
        result = self.engine.backfill_hooks(sid, version, [addition], [{
            "id": "julian_arc", "title": "Наблюдение Джулиана",
            "status": "active", "note": "Джулиан обещал наблюдать",
            "source": HISTORY_QUOTE, "origin_turn": 1
        }])
        self.engine.rollback(sid, result["version"], "hook-turn-2", "Отмена")
        brief = self.engine.director_brief(sid)
        self.assertIn("julian_arc", {a["id"] for a in brief["open_arcs"]})
        self.assertIn("julian_watch", {h["id"] for h in brief["unresolved_hooks"]})

    def test_commit_rejects_orphan_hook(self):
        sid = self.make()
        with self.assertRaisesRegex(story_hooks.HookProblem, "existing open arc"):
            self.turn(sid, 1, HISTORY_QUOTE, [
                hook("julian_watch", "Обещание Джулиана", "commitment",
                     HISTORY_QUOTE, arc_id="absent_arc")
            ])
        self.assertEqual(self.engine.status(sid)["turn"], 0)

    def test_recorded_arrival_assessment_and_cache_survive_restart_and_rollback(self):
        sid = self.make()
        brief = self.engine.director_brief(sid)
        packet = self.engine.prepare(sid, "Приходит Селин", ["adrian", "seline"],
                                     request_id="seline-first", story_plan=fixture_plan(brief))
        for i in packet["unread_chunk_indices"]:
            self.engine.read_chunk(sid, packet["packet_id"], i)
        next_state = state(1)
        next_state["present"]["seline"] = empty_actor()
        result = self.engine.commit(sid, {
            "request_id": "seline-first", "expected_version": packet["version"],
            "packet_id": packet["packet_id"], "chunk_hashes": packet["chunk_hashes"],
            "reader_text": "Приходит Селин", "narration": "Селин вошла в комнату.",
            "participants": ["adrian", "seline"], "state": next_state,
            "knowledge": [], "events": [], "relations": [],
        })
        restarted = Engine(self.engine.root, self.engine.instructions)
        brief = restarted.director_brief(sid)
        self.assertEqual(brief["first_recorded_participation"]["seline"], 1)
        self.assertEqual(brief["appearance_review"], ["seline_arrival"])
        self.assertIn("hook:seline_arrival", brief["plan_priority_ids"])
        with self.assertRaisesRegex(story_hooks.HookProblem, "flagged obligation"):
            bad = fixture_plan(brief)
            bad["priority"] = {}
            restarted.prepare(sid, "Дальше", [], request_id="next", story_plan=bad)
        restarted.prepare(sid, "Дальше", [], request_id="next", story_plan=fixture_plan(brief))
        context = json.loads((self.engine.root / sid / "system/packet.json").read_text())
        self.assertEqual(context["story_health"]["appearance_review_ids"], ["seline_arrival"])
        restarted.rollback(sid, result["version"], "seline-first", "Отмена появления")
        self.assertNotIn("seline", restarted.director_brief(sid)["first_recorded_participation"])

    def test_structured_conditions_become_due_without_forcing_scene(self):
        quote = "Фотографии Ханы запланированы на 10 мая."
        original = CANON + quote
        rows = story_hooks.parse(story_hooks.seed([
            hook("hana_photo", "Фотографии", "commitment", quote,
                 status="blocked", trigger="date:2026-05-10"),
            hook("later_turn", "Следующий урок", "commitment", quote,
                 status="blocked", trigger="turn:60"),
            hook("after_photo", "Последствия", "consequence", quote,
                 status="blocked", trigger="hook:hana_photo"),
        ], original, {"seline"}))
        overview = story_hooks.director_brief(rows, 58, world_datetime="2026-05-05T19:00:00")
        self.assertEqual(overview["conditions_due"], [])
        overview = story_hooks.director_brief(rows, 59, world_datetime="2026-05-10T00:00:00")
        self.assertEqual(set(overview["conditions_due"]), {"hana_photo", "later_turn"})
        self.assertEqual(next(h for h in rows if h["id"] == "hana_photo")["status"], "blocked")
        rows[0 if rows[0]["id"] == "hana_photo" else next(
            i for i, h in enumerate(rows) if h["id"] == "hana_photo")]["status"] = "closed"
        overview = story_hooks.director_brief(rows, 60, world_datetime="2026-05-11T00:00:00")
        self.assertIn("after_photo", overview["conditions_due"])

    def test_audit_rotates_old_closures_without_dropping_current_hooks(self):
        # Even 1500 old resolutions inside a still-open long arc do not
        # become 1500 mandatory prose reports at every short audit.
        rows = [{"id": f"old_{i:04d}", "arc_id": "live_arc", "status": "closed",
                 "kind": "mystery", "window_end": None, "last_turn": 1}
                for i in range(1500)]
        rows.append({"id": "live_hook", "arc_id": "live_arc", "status": "active",
                     "kind": "mystery", "window_end": None, "last_turn": 1000})
        chosen, cursor = story_hooks.audit_selection(rows, [{"id": "live_arc"}], 2000, 15)
        self.assertEqual([h["id"] for h in chosen], ["live_hook"])
        first, cursor = story_hooks.audit_selection(rows, [{"id": "live_arc"}], 2000, 60, cursor)
        second, cursor = story_hooks.audit_selection(rows, [{"id": "live_arc"}], 2060, 60, cursor)
        self.assertEqual(len(first), 65)
        self.assertEqual(len(second), 65)
        self.assertEqual({h["id"] for h in first} & {h["id"] for h in second}, {"live_hook"})

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
            **{w["id"]: "Срок автора проверен по анкете и истории."
               for w in story_hooks.author_window_candidates(CANON, 15)},
        }
        result = self.engine.audit(sid, p["version"], p["packet_id"],
                                   p["chunk_hashes"], report)
        self.assertTrue(result["audited"])
        self.assertEqual(result["plot_findings"]["open_hook_count"], 2)
        self.assertEqual(result["plot_findings"]["open_arc_count"], 1)


if __name__ == "__main__":
    unittest.main()
