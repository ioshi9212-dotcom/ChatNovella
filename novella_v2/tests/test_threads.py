"""Unresolved threads: bounded cues without plot quotas, timers, or forced closure."""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novella_v2.engine import Engine, EngineError, empty_actor, digest
from novella_v2.tests.test_story_hooks import fixture_plan
from novella_v2.threads import HEADER, MAX_OPEN, ThreadProblem, parse, render, seed, apply


def state(turn=0):
    return {"turn": turn, "datetime": "2026-10-01T18:00:00",
            "location": "Комната", "pov": empty_actor(),
            "present": {"adrian": empty_actor()}, "remote": {},
            "objects": [], "unfinished": []}


class TestOpenThreads(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        instructions = root / "instructions"
        instructions.mkdir()
        (instructions / "rules.md").write_text("Технические правила", encoding="utf-8")
        (instructions / "skin_builder.md").write_bytes(b"")
        self.engine = Engine(root / "storage", instructions)
        self.original = "Город. У Адриана пропало письмо. Он не сказал, кто его взял.\n"
        self.blocks = [{"start": 0, "end": len(self.original), "target": "novel"}]
        self.sid = self._create()
        status = self.engine.status(self.sid)
        self.engine.approve(self.sid, status["version"], status["canon_hash"], "Утверждаю")
        self.engine.set_opening(self.sid, self.engine.status(self.sid)["version"], state())

    def _create(self, initial=None):
        return self.engine.create(self.original, self.blocks,
                                  {"adrian": "Адриан"},
                                  initial_threads=initial)["session_id"]

    def path(self, name):
        return self.engine.root / self.sid / name

    def prepared(self, text="Читаю письмо", kind="turn"):
        plan = fixture_plan(self.engine.director_brief(self.sid)) if kind == "turn" else None
        packet = self.engine.prepare(self.sid, text, ["adrian"], kind, story_plan=plan)
        raw = "".join(self.engine.read_chunk(self.sid, packet["packet_id"], i)["content"]
                      for i in range(packet["chunk_count"]))
        self.assertEqual(digest(raw), packet["sha256"])
        return packet, json.loads(raw)

    def commit(self, n, changes=None, text="Читаю письмо",
               scene="**Адриан** — Я нашёл письмо."):
        packet, payload = self.prepared(text)
        self.assertIn("open_threads.md", payload["files"])
        args = {"request_id": "threads-" + str(n), "expected_version": packet["version"],
                "packet_id": packet["packet_id"], "chunk_hashes": packet["chunk_hashes"],
                "reader_text": text, "narration": scene, "participants": ["adrian"],
                "state": state(n), "knowledge": [], "events": [], "relations": []}
        if changes is not None:
            args["thread_updates"] = changes
        return self.engine.commit(self.sid, args)

    def update(self, ident="missing_letter", status="active",
               note="Адриан обнаружил пропажу.", source="Я нашёл письмо."):
        return {"id": ident, "title": "Пропавшее письмо",
                "status": status, "note": note, "source": source}

    def test_empty_threads_file_always_in_packet(self):
        self.assertEqual(self.path("open_threads.md").read_text(encoding="utf-8"), HEADER)
        p, data = self.prepared()
        self.assertEqual(data["files"]["open_threads.md"], HEADER)
        self.assertNotIn("Skin Builder", json.dumps(data["files"]))
        self.assertGreater(p["chunk_count"], 0)

    def test_canon_preview_also_shows_initial_threads(self):
        initial = [{"id": "letter", "title": "Пропавшее письмо", "status": "not_started",
                    "note": "Пропажа установлена в анкете.",
                    "source": "У Адриана пропало письмо."}]
        second = self._create(initial)
        packet = self.engine.prepare(second, kind="canon")
        data = json.loads("".join(
            self.engine.read_chunk(second, packet["packet_id"], i)["content"]
            for i in range(packet["chunk_count"])))
        lines = parse(data["files"]["open_threads.md"])
        self.assertEqual(lines[0]["status"], "not_started")
        self.assertEqual(lines[0]["turn"], 0)

    def test_initial_threads_require_exact_author_source(self):
        second = self._create([{"id": "letter", "title": "Пропавшее письмо",
                               "status": "not_started", "note": "Будет раскрыто",
                               "source": "Этого не существует в оригинале."}])
        self.assertEqual(parse((self.engine.root / second / "open_threads.md")
                               .read_text(encoding="utf-8")), [])
        self.assertTrue((self.engine.root / second / "original.txt").exists())

    def test_new_thread_appears_every_subsequent_game_packet(self):
        self.commit(1, [self.update()])
        p, context = self.prepared("Что с письмом?")
        active = parse(context["files"]["open_threads.md"])
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["status"], "active")
        self.assertEqual(active[0]["turn"], 1)
        self.assertEqual(active[0]["id"], "missing_letter")
        self.assertTrue(p["chunk_count"] > 0)

    def test_updating_existing_thread_replaces_note_not_appends(self):
        self.commit(1, [self.update()])
        next_change = self.update(note="Адриан отказался говорить о письме.",
                                  source="Я не хочу это обсуждать.")
        self.commit(2, [next_change], text="Что случилось?",
                    scene="**Адриан** — Я не хочу это обсуждать.")
        current = self.path("open_threads.md").read_text(encoding="utf-8")
        self.assertEqual(len(parse(current)), 1)
        self.assertNotIn("обнаружил пропажу", current)
        self.assertIn("отказался говорить", current)
        self.assertEqual(parse(current)[0]["turn"], 2)

    def test_closed_thread_removed_from_working_packet_but_archived(self):
        self.commit(1, [self.update()])
        result = self.commit(2, [self.update(
            status="closed", note="Адриан узнал судьбу письма.",
            source="Письмо забрал Марк.")],
            text="Кто взял?", scene="**Адриан** — Письмо забрал Марк.")
        self.assertTrue(result["saved"])
        self.assertEqual(self.path("open_threads.md").read_text(encoding="utf-8"), HEADER)
        _, context = self.prepared("Поговорим о другом")
        self.assertEqual(context["files"]["open_threads.md"], HEADER)
        self.assertEqual(self.engine.history(self.sid)[-1]["thread_updates"][0]["status"],
                         "closed")

    def test_undeveloped_scene_does_not_change_file(self):
        self.commit(1, [self.update()])
        before = self.path("open_threads.md").read_bytes()
        self.commit(2, text="Молчу", scene="**Адриан** — Хорошо, помолчим.")
        self.assertEqual(self.path("open_threads.md").read_bytes(), before)
        self.assertEqual(self.engine.history(self.sid)[-1]["thread_updates"], [])

    def test_pov_refusal_never_forces_close(self):
        self.commit(1, [self.update()])
        for n in range(2, 9):
            self.commit(n, text="Я не хочу обсуждать письмо",
                        scene="**Адриан** — Это твоё решение.")
        self.assertEqual(parse(self.path("open_threads.md").read_text(encoding="utf-8"))[0]["status"],
                         "active")
        self.assertEqual(len(self.engine.history(self.sid)), 8)

    def test_thread_update_requires_exact_quote_from_saved_scene(self):
        p, _ = self.prepared()
        body = {"request_id": "bad", "expected_version": p["version"],
                "packet_id": p["packet_id"], "chunk_hashes": p["chunk_hashes"],
                "reader_text": "Читаю письмо",
                "narration": "**Адриан** — Молчу.",
                "participants": ["adrian"], "state": state(1),
                "knowledge": [], "events": [], "relations": [],
                "thread_updates": [self.update()]}
        with self.assertRaisesRegex(ThreadProblem, "exact excerpt"):
            self.engine.commit(self.sid, body)
        self.assertEqual(self.engine.status(self.sid)["turn"], 0)

    def test_cannot_close_unknown_thread(self):
        with self.assertRaisesRegex(ThreadProblem, "nonexistent"):
            self.commit(1, [self.update(status="closed")])

    def test_cannot_repeat_same_update_without_change(self):
        self.commit(1, [self.update()])
        with self.assertRaisesRegex(ThreadProblem, "unchanged thread"):
            self.commit(2, [self.update()], text="Повтор")

    def test_rollback_restores_open_threads(self):
        first = self.commit(1, [self.update()])
        self.engine.rollback(self.sid, first["version"], "threads-1", "Отмена")
        self.assertEqual(self.path("open_threads.md").read_text(encoding="utf-8"), HEADER)
        self.assertEqual(self.engine.history(self.sid), [])

    def test_rollback_closure_restores_open_thread(self):
        self.commit(1, [self.update()])
        second = self.commit(2, [self.update(status="closed",
                                           note="Ответ был найден.",
                                           source="Письмо забрал Марк.")],
                             text="Кто забрал?", scene="**Адриан** — Письмо забрал Марк.")
        self.engine.rollback(self.sid, second["version"], "threads-2", "Отмена")
        self.assertEqual(len(parse(self.path("open_threads.md").read_text(encoding="utf-8"))), 1)

    def test_bounded_file_and_no_duplicate_ids(self):
        entries = [{"id": "arc_" + str(n), "title": "Линия №" + str(n),
                    "note": "Ожидает исхода", "status": "not_started", "turn": 0}
                   for n in range(MAX_OPEN)]
        self.assertEqual(len(parse(render(entries))), MAX_OPEN)
        with self.assertRaisesRegex(ThreadProblem, "Too many unresolved"):
            render(entries + [{**entries[0], "id": "extra_1"}])
        with self.assertRaisesRegex(ThreadProblem, "Duplicate thread"):
            render(entries[:-1] + entries[:1])

    def test_corrupt_active_file_stops_next_turn(self):
        self.path("open_threads.md").write_text("# Broken", encoding="utf-8")
        with self.assertRaisesRegex(ThreadProblem, "Invalid open_threads"):
            self.prepared()

    def test_60_turn_history_still_remembers_unresolved_arc(self):
        self.commit(1, [self.update()])
        for n in range(2, 16):
            self.commit(n, text="Тихий ход " + str(n),
                        scene="**Адриан** — Просто разговариваем.")
        self.assertEqual(self.engine.status(self.sid)["audit_due"], 15)
        p, context = self.prepared("", kind="audit")
        self.assertIn("Пропавшее письмо", context["files"]["open_threads.md"])
        report = {key: "Проверено" for key in
                  ("continuity", "state", "knowledge", "timeline", "relationships")}
        report["story"] = {"arc:missing_letter": "Письмо остаётся не найденным, линия активна."}
        self.engine.audit(self.sid, p["version"], p["packet_id"], p["chunk_hashes"], report)
        self.assertEqual(len(parse(self.path("open_threads.md").read_text(encoding="utf-8"))), 1)

    def test_legacy_v2_session_without_file_shows_empty_and_migrates_on_change(self):
        self.path("open_threads.md").unlink()
        packet, context = self.prepared()
        self.assertEqual(context["files"]["open_threads.md"], HEADER)
        self.commit(1, [self.update()])
        self.assertTrue(self.path("open_threads.md").exists())


if __name__ == "__main__":
    unittest.main()
