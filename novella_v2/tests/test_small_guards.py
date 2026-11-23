"""Compact, focused regression tests for V2 anti-loop and persistence guards."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from novella_v2.engine import (
    Engine, EngineError, _append_descriptor, atomic, dump, empty_actor,
)


def state(n):
    return {
        "turn": n, "datetime": "2026-09-12T18:00:00",
        "location": "У окна", "pov": empty_actor(),
        "present": {"adrian": empty_actor()}, "remote": {},
        "objects": [], "unfinished": [],
    }


class TestSmallGuards(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        instruction = root / "rules"
        instruction.mkdir()
        (instruction / "rules.md").write_text("Правила", encoding="utf-8")
        (instruction / "skin_builder.md").write_text("Оригинальный Skin Builder", encoding="utf-8")
        self.engine = Engine(root / "storage", instruction)
        self.source = "Город.\nАдриан грубый.\n"
        a = self.source.index("Адриан")
        result = self.engine.create(
            self.source, [{"start": 0, "end": a, "target": "novel"},
                          {"start": a, "end": len(self.source), "target": "adrian"}],
            {"adrian": "Адриан"})
        self.sid = result["session_id"]
        self.engine.approve(self.sid, result["version"],
                            result["canon_hash"], "Подтверждаю")
        self.engine.set_opening(self.sid, self.engine.status(self.sid)["version"], state(0))

    def root(self, name):
        return self.engine.root / self.sid / name

    def prepare(self, reader="Привет", request_id="req-1"):
        return self.engine.prepare(self.sid, reader_text=reader,
                                   participants=["adrian"], request_id=request_id)

    def finish(self, packet):
        for i in range(packet["chunk_count"]):
            self.engine.read_chunk(self.sid, packet["packet_id"], i)

    def payload(self, packet, n=1, reader="Привет", request_id="req-1",
                narration="**Адриан** — Да."):
        return {
            "request_id": request_id, "expected_version": packet["version"],
            "packet_id": packet["packet_id"], "chunk_hashes": packet["chunk_hashes"],
            "reader_text": reader, "narration": narration, "participants": ["adrian"],
            "state": state(n), "knowledge": [], "events": [], "relations": [],
        }

    def test_prepare_reuses_exact_packet_without_rewriting_file(self):
        first = self.prepare()
        self.assertFalse(first["reused_pending_packet"])
        p = self.root("system/packet.json")
        timestamp = p.stat().st_mtime_ns
        second = self.prepare()
        self.assertEqual(second["packet_id"], first["packet_id"])
        self.assertTrue(second["reused_pending_packet"])
        self.assertEqual(second["next_chunk_index"], 0)
        self.assertEqual(p.stat().st_mtime_ns, timestamp)
        self.assertEqual(second["unread_chunk_indices"], list(range(first["chunk_count"])))
        self.finish(second)
        third = self.prepare()
        self.assertTrue(third["all_chunks_read"])
        self.assertIsNone(third["next_chunk_index"])
        self.assertEqual(third["unread_chunk_indices"], [])
        self.assertEqual(p.stat().st_mtime_ns, timestamp)

    def test_large_packet_resumes_only_unread_chunks_after_repeated_prepare(self):
        source = "Город. " + "Ж" * 80_000 + "\nАдриан грубый.\n"
        a = source.index("Адриан")
        new = self.engine.create(source, [
            {"start": 0, "end": a, "target": "novel"},
            {"start": a, "end": len(source), "target": "adrian"},
        ], {"adrian": "Адриан"})
        sid = new["session_id"]
        self.engine.approve(sid, new["version"], new["canon_hash"], "Утверждаю")
        self.engine.set_opening(sid, self.engine.status(sid)["version"], state(0))
        first = self.engine.prepare(sid, "Hi", ["adrian"], request_id="large-1")
        self.assertGreater(first["chunk_count"], 2)
        part0 = self.engine.read_chunk(sid, first["packet_id"], 0)
        self.assertEqual(part0["next_chunk_index"], 1)
        repeated = self.engine.prepare(sid, "Hi", ["adrian"], request_id="large-1")
        self.assertEqual(repeated["packet_id"], first["packet_id"])
        self.assertTrue(repeated["reused_pending_packet"])
        self.assertEqual(repeated["unread_chunk_indices"], list(range(1, first["chunk_count"])))
        self.assertEqual(repeated["next_chunk_index"], 1)
        for i in repeated["unread_chunk_indices"]:
            self.engine.read_chunk(sid, first["packet_id"], i)
        done = self.engine.prepare(sid, "Hi", ["adrian"], request_id="large-1")
        self.assertTrue(done["all_chunks_read"])
        self.assertEqual(done["unread_chunk_indices"], [])

    def test_saved_request_is_returned_even_while_audit_is_due(self):
        for n in range(1, 16):
            reader = "ход " + str(n)
            req = "audit-prep-" + str(n)
            packet = self.prepare(reader=reader, request_id=req)
            self.finish(packet)
            result = self.engine.commit(self.sid, self.payload(
                packet, n=n, reader=reader, request_id=req))
        self.assertEqual(result["audit_due"], 15)
        retry = self.engine.prepare(self.sid, "ход 15", ["adrian"],
                                    request_id="audit-prep-15")
        self.assertTrue(retry["already_committed_duplicate"])
        self.assertEqual(retry["turn"], 15)
        self.assertEqual(len(self.engine.history(self.sid)), 15)
        with self.assertRaisesRegex(EngineError, "Audit is required"):
            self.prepare(reader="ход 16", request_id="audit-prep-16")

    def test_new_input_or_new_request_issues_new_packet(self):
        old = self.prepare()
        changed = self.prepare(reader="Другой ввод")
        self.assertNotEqual(changed["packet_id"], old["packet_id"])
        new_request = self.prepare(reader="Другой ввод", request_id="req-2")
        self.assertNotEqual(new_request["packet_id"], changed["packet_id"])

    def test_same_reader_text_with_new_request_is_new_game_turn(self):
        first = self.prepare()
        self.finish(first)
        self.engine.commit(self.sid, self.payload(first))
        second = self.prepare(request_id="req-2")
        self.assertFalse(second.get("already_committed_duplicate"))
        self.finish(second)
        answer = self.engine.commit(
            self.sid, self.payload(second, n=2, request_id="req-2"))
        self.assertEqual(answer["turn"], 2)
        self.assertEqual(len(self.engine.history(self.sid)), 2)

    def test_retry_after_commit_returns_exact_saved_scene_without_prepare(self):
        first = self.prepare()
        self.finish(first)
        scene = "**Адриан** — Здравствуй. 🌙\n(Она молчит.)"
        result = self.engine.commit(self.sid, self.payload(first, narration=scene))
        duplicate = self.prepare()
        self.assertTrue(duplicate["already_committed_duplicate"])
        self.assertEqual(duplicate["narration"], scene)
        self.assertEqual(duplicate["turn"], result["turn"])
        self.assertNotIn("packet_id", duplicate)
        self.assertEqual(len(self.engine.history(self.sid)), 1)

    def test_recovered_duplicate_after_engine_restart(self):
        packet = self.prepare()
        self.finish(packet)
        scene = "**Адриан** — Возвращаюсь."
        self.engine.commit(self.sid, self.payload(packet, narration=scene))
        restarted = Engine(self.engine.root, self.engine.instructions)
        result = restarted.prepare(self.sid, "Привет", ["adrian"],
                                   request_id="req-1")
        self.assertTrue(result["already_committed_duplicate"])
        self.assertEqual(result["narration"], scene)

    def test_repeating_id_with_altered_user_text_is_rejected(self):
        packet = self.prepare()
        self.finish(packet)
        self.engine.commit(self.sid, self.payload(packet))
        with self.assertRaisesRegex(EngineError, "different reader input"):
            self.prepare(reader="Иной текст")

    def test_wrong_request_identity_cannot_commit_other_packet(self):
        packet = self.prepare()
        self.finish(packet)
        with self.assertRaisesRegex(EngineError, "committed request ID differ"):
            self.engine.commit(self.sid, self.payload(packet, request_id="req-other"))

    def test_read_chunk_uses_byte_ranges_not_full_packet_reloads(self):
        first = self.prepare()
        with patch.object(Engine, "_read", wraps=Engine._read) as monitor:
            self.engine.read_chunk(self.sid, first["packet_id"], 0)
            read_paths = [x.args[1] for x in monitor.call_args_list
                          if len(x.args) >= 2]
            self.assertNotIn("system/packet.json", read_paths)

    def test_packet_rebuilt_only_when_skin_builder_changes(self):
        first = self.prepare()
        builder = self.engine.instructions / "skin_builder.md"
        builder.write_text("Другая версия Skin Builder", encoding="utf-8")
        second = self.prepare()
        self.assertNotEqual(first["packet_id"], second["packet_id"])
        self.assertFalse(second["reused_pending_packet"])

    def test_orphaned_packet_after_crash_is_recreated_not_reused_forever(self):
        old = self.prepare()
        atomic(self.root("system/packet.json"), "разбитый пакет после сбоя")
        fresh = self.prepare()
        self.assertNotEqual(fresh["packet_id"], old["packet_id"])
        self.assertFalse(fresh["reused_pending_packet"])
        part = self.engine.read_chunk(self.sid, fresh["packet_id"], 0)
        self.assertEqual(part["packet_id"], fresh["packet_id"])
        self.assertTrue(part["content"].startswith("{"))

    def test_packet_tampered_after_last_chunk_cannot_be_committed(self):
        packet = self.prepare()
        self.finish(packet)
        atomic(self.root("system/packet.json"), "изменено после чтения")
        with self.assertRaisesRegex(EngineError, "Prepared context bytes changed"):
            self.engine.commit(self.sid, self.payload(packet))
        self.assertEqual(self.engine.status(self.sid)["turn"], 0)

    def test_packet_invalidated_by_external_knowledge_edit(self):
        first = self.prepare()
        knowledge = self.root("knowledge/adrian.md")
        knowledge.write_text(knowledge.read_text(encoding="utf-8") + "Сторонняя правка\n",
                             encoding="utf-8")
        second = self.prepare()
        self.assertNotEqual(first["packet_id"], second["packet_id"])
        self.assertFalse(second["reused_pending_packet"])

    def test_commit_rejects_after_prepared_sources_mutate(self):
        first = self.prepare()
        self.finish(first)
        knowledge = self.root("knowledge/adrian.md")
        knowledge.write_text("# Новые знания: adrian\nПостороннее изменение",
                             encoding="utf-8")
        with self.assertRaisesRegex(EngineError, "changed after preparing"):
            self.engine.commit(self.sid, self.payload(first))
        self.assertEqual(len(self.engine.history(self.sid)), 0)

    def test_main_canon_edit_is_rejected(self):
        novel = self.root("novel.md")
        novel.write_text(novel.read_text(encoding="utf-8") + "\nПодмена анкеты",
                         encoding="utf-8")
        with self.assertRaisesRegex(EngineError, "Main questionnaire changed"):
            self.prepare()

    def test_character_card_edit_is_rejected(self):
        card = self.root("characters/adrian.md")
        card.write_text(card.read_text(encoding="utf-8") + "\nОтменить характер",
                        encoding="utf-8")
        with self.assertRaisesRegex(EngineError, "character card changed"):
            self.prepare()

    def test_corrupt_saved_physical_state_is_rejected_on_prepare(self):
        current = json.loads(self.root("state.md").read_text(encoding="utf-8"))
        current["turn"] = 999
        atomic(self.root("state.md"), dump(current))
        with self.assertRaisesRegex(EngineError, "State turn does not match"):
            self.prepare()

    def test_long_exactly_repeated_scene_warns_without_blocking(self):
        long_text = "**Адриан** — " + ("Я пришёл сюда и всё объяснил. " * 16)
        first = self.prepare()
        self.finish(first)
        first_result = self.engine.commit(self.sid, self.payload(first, narration=long_text))
        self.assertEqual(first_result["warnings"], [])
        second = self.prepare(reader="Продолжай", request_id="req-2")
        self.finish(second)
        second_result = self.engine.commit(
            self.sid, self.payload(second, n=2, reader="Продолжай",
                                   request_id="req-2", narration=long_text))
        self.assertEqual(second_result["warnings"], ["SCENE_NEAR_DUPLICATE"])
        self.assertEqual(len(self.engine.history(self.sid)), 2)
        self.assertEqual(self.engine.history(self.sid)[-1]["warnings"],
                         ["SCENE_NEAR_DUPLICATE"])

    def test_brief_repeated_dialogue_does_not_warn(self):
        first = self.prepare()
        self.finish(first)
        self.engine.commit(self.sid, self.payload(first, narration="**Адриан** — Да."))
        second = self.prepare(reader="Ещё", request_id="req-2")
        self.finish(second)
        result = self.engine.commit(self.sid, self.payload(second, n=2, reader="Ещё",
                                                           request_id="req-2"))
        self.assertEqual(result["warnings"], [])

    def test_archive_is_appended_without_replacing_file(self):
        history = self.root("history.jsonl")
        initial_inode = history.stat().st_ino
        for n in range(1, 6):
            reader = "ход " + str(n)
            request = "id-" + str(n)
            p = self.prepare(reader=reader, request_id=request)
            self.finish(p)
            self.engine.commit(self.sid, self.payload(
                p, n=n, reader=reader, request_id=request))
            self.assertEqual(history.stat().st_ino, initial_inode)
        self.assertEqual(len(self.engine.history(self.sid)), 5)

    def test_partial_append_after_crash_is_replayed_once(self):
        target = self.root("history.jsonl")
        descriptor = _append_descriptor(target, '{"kind":"recovery"}\n')
        meta_file = self.root("system/meta.json")
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        new = {**meta, "version": meta["version"] + 1}
        pending = {"files": {}, "appends": {"history.jsonl": descriptor}, "next": new}
        atomic(meta_file, dump({**meta, "pending": pending}))
        with target.open("ab") as handle:
            handle.write(b'{"kin')
        self.assertEqual(self.engine.status(self.sid)["version"], new["version"])
        self.assertEqual(target.read_bytes(), b'{"kind":"recovery"}\n')
        self.assertEqual(self.engine.status(self.sid)["version"], new["version"])
        self.assertEqual(target.read_bytes().count(b'"recovery"'), 1)

    def test_wrong_archive_tail_causes_fail_closed_recovery(self):
        target = self.root("history.jsonl")
        descriptor = _append_descriptor(target, '{"kind":"recovery"}\n')
        meta_file = self.root("system/meta.json")
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        new = {**meta, "version": meta["version"] + 1}
        atomic(meta_file, dump({**meta, "pending": {
            "files": {}, "appends": {"history.jsonl": descriptor}, "next": new}}))
        with target.open("ab") as handle:
            handle.write(b"CORRUPT_GARBAGE_OUTSIDE_TRANSACTION")
        with self.assertRaisesRegex(EngineError, "Archive size changed"):
            self.engine.status(self.sid)


if __name__ == "__main__":
    unittest.main()
