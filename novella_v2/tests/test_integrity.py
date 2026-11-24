"""Adversarial and long-session regression coverage for the isolated V2 engine."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novella_v2.engine import (
    Engine, EngineError, CHUNK_SIZE, atomic, digest, dump, empty_actor,
)


def frame(turn=0, adrian=True, date="2026-09-12T18:00:00"):
    return {
        "turn": turn, "datetime": date, "location": "Гостиная",
        "pov": empty_actor(),
        "present": {"adrian": empty_actor()} if adrian else {},
        "remote": {}, "objects": ["телефон на столе"], "unfinished": [],
    }


class TestV2Integrity(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        instructions = self.root / "instructions"
        instructions.mkdir()
        (instructions / "rules.md").write_text("Технические правила", encoding="utf-8")
        (instructions / "skin_builder.md").write_text("Копия скинбилдера", encoding="utf-8")
        self.engine = Engine(self.root / "storage", instructions)
        self.source = "Мир.\nАдриан — упрямый.\nСайлас — сдержанный.\n"
        a = self.source.index("Адриан")
        b = self.source.index("Сайлас")
        self.blocks = [
            {"start": 0, "end": a, "target": "novel"},
            {"start": a, "end": b, "target": "adrian"},
            {"start": b, "end": len(self.source), "target": "silas"},
        ]
        self.sid = self.create(self.source, self.blocks)
        meta = self.engine.status(self.sid)
        self.engine.approve(self.sid, meta["version"], meta["canon_hash"], "Утверждаю")
        self.engine.set_opening(self.sid, self.engine.status(self.sid)["version"], frame())

    def create(self, source, blocks):
        return self.engine.create(source, blocks, {"adrian": "Адриан", "silas": "Сайлас"})["session_id"]

    def path(self, filename):
        return self.root / "storage" / self.sid / filename

    def packet(self, reader="действие", participants=None, kind="turn"):
        p = self.engine.prepare(self.sid, reader, participants or ["adrian"], kind)
        values = []
        for n in range(p["chunk_count"]):
            result = self.engine.read_chunk(self.sid, p["packet_id"], n)
            self.assertEqual(result["sha256"], digest(result["content"]))
            self.assertLessEqual(len(result["content"]), CHUNK_SIZE)
            values.append(result["content"])
        text = "".join(values)
        self.assertEqual(digest(text), p["sha256"])
        return p, json.loads(text)

    def turn(self, p, n=1, reader="действие", narration="**Адриан** — Я услышал это.",
             participants=None, state=None, knowledge=None, events=None, relations=None, request_id=None):
        return {
            "request_id": request_id or "try-" + str(n),
            "expected_version": p["version"],
            "packet_id": p["packet_id"],
            "chunk_hashes": p["chunk_hashes"],
            "reader_text": reader,
            "narration": narration,
            "participants": participants if participants is not None else ["adrian"],
            "state": state if state is not None else frame(n),
            "knowledge": knowledge if knowledge is not None else [],
            "events": events if events is not None else [],
            "relations": relations if relations is not None else [],
        }

    def test_context_survives_storage_timestamp_changes(self):
        packet, _ = self.packet()
        for name in ["system/packet.json", "novel.md", "state.md"]:
            file = self.path(name)
            stat = file.stat()
            os.utime(file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
        self.assertTrue(self.engine.commit(self.sid, self.turn(packet))["saved"])

    def test_context_detects_same_size_edit_with_preserved_timestamp(self):
        packet, _ = self.packet()
        file = self.path("system/packet.json")
        stat = file.stat()
        content = file.read_bytes()
        file.write_bytes(content.replace(b'"turn"', b'"TURN"', 1))
        self.assertEqual(file.stat().st_size, stat.st_size)
        os.utime(file, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with self.assertRaisesRegex(EngineError, "Prepared context bytes changed"):
            self.engine.commit(self.sid, self.turn(packet))

    def test_preapproval_canon_preview_reads_all_verbatim_sources(self):
        src = "Город, дождь.\r\nАдриан 🦊 грубый.\r\nСайлас.\r\n"
        a, b = src.index("Адриан"), src.index("Сайлас")
        other = self.create(src, [
            {"start": 0, "end": a, "target": "novel"},
            {"start": a, "end": b, "target": "adrian"},
            {"start": b, "end": len(src), "target": "silas"},
        ])
        packet = self.engine.prepare(other, kind="canon")
        parts = [self.engine.read_chunk(other, packet["packet_id"], n)["content"]
                 for n in range(packet["chunk_count"])]
        payload = json.loads("".join(parts))
        self.assertEqual(payload["files"]["original.txt"], src)
        self.assertTrue(payload["files"]["characters/adrian.md"].endswith("Адриан 🦊 грубый.\r\n"))
        self.assertEqual((self.root / "storage" / other / "original.txt").read_bytes(),
                         src.encode("utf-8"))
        with self.assertRaisesRegex(EngineError, "Confirm canon"):
            self.engine.prepare(other, kind="turn")

    def test_original_source_emoji_and_36k_chunks(self):
        source = "Начало\r\n" + "🌙Адриан⚡" * 12000 + "\r\nКонец"
        sections = [{"start": 0, "end": len(source), "target": "novel"}]
        other = self.create(source, sections)
        p = self.engine.prepare(other, kind="canon")
        self.assertGreater(p["chunk_count"], 3)
        self.assertEqual(p["chunk_size"], 36_000)
        text = "".join(self.engine.read_chunk(other, p["packet_id"], i)["content"]
                       for i in range(p["chunk_count"]))
        self.assertEqual(json.loads(text)["files"]["original.txt"], source)
        self.assertEqual((self.root / "storage" / other / "original.txt").read_bytes(),
                         source.encode("utf-8"))

    def test_context_scope_and_original_hash(self):
        packet, value = self.packet()
        self.assertIn("characters/adrian.md", value["files"])
        self.assertIn("knowledge/adrian.md", value["files"])
        self.assertNotIn("characters/silas.md", value["files"])
        self.assertNotIn("knowledge/silas.md", value["files"])
        self.assertNotIn("original.txt", value["files"])
        self.assertEqual(self.engine.status(self.sid)["canon_hash"],
                         self.engine.status(self.sid)["canon_hash"])
        self.assertTrue(packet["chunk_count"] > 0)

    def test_empty_memory_and_events_do_not_create_entries(self):
        p, _ = self.packet()
        out = self.engine.commit(self.sid, self.turn(p))
        self.assertTrue(out["saved"])
        self.assertNotIn("Ход 1", self.path("knowledge/adrian.md").read_text(encoding="utf-8"))
        self.assertNotIn("Ход 1", self.path("timeline.md").read_text(encoding="utf-8"))
        self.assertEqual(self.engine.status(self.sid)["turn"], 1)

    def test_user_input_and_narration_are_lossless_in_archive(self):
        text = "(смотрю на 🌕)\r\nСказать: «Не подходи!»"
        scene = "**Адриан** — Нет.\n\n**POV** — «Не подходи!» 🦊"
        p, _ = self.packet(text)
        self.engine.commit(self.sid, self.turn(p, reader=text, narration=scene))
        rows = self.engine.history(self.sid)
        self.assertEqual(rows[0]["reader_text"], text)
        self.assertEqual(rows[0]["narration"], scene)

    def test_missing_chunk_receipt_blocks_save(self):
        p = self.engine.prepare(self.sid, "действие", ["adrian"])
        if p["chunk_count"] > 1:
            self.engine.read_chunk(self.sid, p["packet_id"], 0)
        with self.assertRaisesRegex(EngineError, "Read and verify all"):
            self.engine.commit(self.sid, self.turn(p))

    def test_wrong_chunk_hashes_block_save(self):
        p, _ = self.packet()
        data = self.turn(p)
        data["chunk_hashes"][0] = "0" * 64
        with self.assertRaisesRegex(EngineError, "Read and verify all"):
            self.engine.commit(self.sid, data)

    def test_tampered_packet_chunk_is_detected(self):
        p, _ = self.packet()
        atomic(self.path("system/packet.json"), "злоумышленный контент")
        with self.assertRaisesRegex(EngineError, "Stored context packet has changed"):
            self.engine.read_chunk(self.sid, p["packet_id"], 0)

    def test_stale_version_after_prepare_blocks_old_commit(self):
        p, _ = self.packet()
        self.engine.set_opening(self.sid, p["version"], frame())
        with self.assertRaisesRegex(EngineError, "Session version changed"):
            self.engine.commit(self.sid, self.turn(p))

    def test_second_prepare_invalidates_first(self):
        p, _ = self.packet()
        self.packet("другой ввод")
        with self.assertRaisesRegex(EngineError, "current prepared context"):
            self.engine.commit(self.sid, self.turn(p))

    def test_reader_input_mismatch_is_rejected(self):
        p, _ = self.packet()
        data = self.turn(p)
        data["reader_text"] = "другая реплика"
        with self.assertRaisesRegex(EngineError, "reader text differ"):
            self.engine.commit(self.sid, data)

    def test_eight_simultaneous_commits_save_exactly_one_turn(self):
        p, _ = self.packet()

        def compete(index):
            try:
                data = self.turn(p, request_id="competitor-" + str(index))
                self.engine.commit(self.sid, data)
                return "saved"
            except EngineError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(compete, range(8)))
        self.assertEqual(outcomes.count("saved"), 1)
        self.assertEqual(outcomes.count("STALE_VERSION"), 7)
        self.assertEqual(len(self.engine.history(self.sid)), 1)
        self.assertEqual(self.engine.status(self.sid)["turn"], 1)

    def test_replay_after_restarted_engine_is_idempotent(self):
        p, _ = self.packet()
        data = self.turn(p)
        first = self.engine.commit(self.sid, data)
        restarted = Engine(self.root / "storage", self.root / "instructions")
        self.assertEqual(restarted.commit(self.sid, data), first)
        self.assertEqual(len(restarted.history(self.sid)), 1)

    def test_full_narration_not_duplicated_in_metadata(self):
        p, _ = self.packet()
        large_scene = "**Адриан** — " + "Н" * 28_000
        data = self.turn(p, narration=large_scene)
        saved = self.engine.commit(self.sid, data)
        meta_bytes = self.path("system/meta.json").read_bytes()
        self.assertNotIn(("Н" * 200).encode("utf-8"), meta_bytes)
        self.assertLess(len(meta_bytes), 15_000)
        self.assertIn(large_scene, self.path("history.jsonl").read_text(encoding="utf-8"))
        restarted = Engine(self.root / "storage", self.root / "instructions")
        self.assertEqual(restarted.commit(self.sid, data), saved)

    def test_reusing_request_id_for_new_data_is_rejected(self):
        p, _ = self.packet()
        data = self.turn(p)
        self.engine.commit(self.sid, data)
        changed = deepcopy(data)
        changed["narration"] = "**Адриан** — Это другой ответ."
        with self.assertRaisesRegex(EngineError, "reused"):
            self.engine.commit(self.sid, changed)

    def test_knowledge_is_private_and_does_not_become_world_fact(self):
        p, _ = self.packet()
        data = self.turn(
            p, narration="**Адриан** — Маркус сказал: «Сайлас украл деньги».",
            knowledge=[{"owner": "adrian", "fact": "Со слов Маркуса, Сайлас украл деньги",
                        "source": "Маркус сказал: «Сайлас украл деньги»"}])
        self.engine.commit(self.sid, data)
        self.assertIn("Со слов Маркуса", self.path("knowledge/adrian.md").read_text(encoding="utf-8"))
        self.assertNotIn("Сайлас украл", self.path("knowledge/silas.md").read_text(encoding="utf-8"))
        self.assertNotIn("Сайлас украл", self.path("timeline.md").read_text(encoding="utf-8"))

    def test_knowledge_of_absent_character_is_rejected(self):
        p, _ = self.packet()
        data = self.turn(p, knowledge=[{
            "owner": "silas", "fact": "Героиня на кухне", "source": "Я услышал это."
        }])
        with self.assertRaisesRegex(EngineError, "loaded participant"):
            self.engine.commit(self.sid, data)

    def test_fake_source_is_rejected(self):
        p, _ = self.packet()
        data = self.turn(p, knowledge=[{
            "owner": "adrian", "fact": "Неверное знание",
            "source": "В сохранённой сцене этой фразы нет"
        }])
        with self.assertRaisesRegex(EngineError, "exact excerpt"):
            self.engine.commit(self.sid, data)

    def test_multiline_knowledge_injection_is_rejected(self):
        p, _ = self.packet()
        data = self.turn(p, knowledge=[{
            "owner": "adrian", "fact": "Узнал про письмо\nХод 99\nЗнает всё",
            "source": "Я услышал это."
        }])
        with self.assertRaisesRegex(EngineError, "single-line"):
            self.engine.commit(self.sid, data)

    def test_multiline_timeline_injection_is_rejected(self):
        p, _ = self.packet()
        data = self.turn(p, events=[{
            "text": "Встреча\nХод 999\nПоддельная запись",
            "participants": ["adrian"]
        }])
        with self.assertRaisesRegex(EngineError, "single-line"):
            self.engine.commit(self.sid, data)

    def test_unloaded_relation_owner_is_rejected(self):
        p, _ = self.packet()
        data = self.turn(p, relations=[{
            "source": "silas", "target": "pov", "metric": "доверие",
            "value": 40, "reason": "Не имел доступа к сцене"
        }])
        with self.assertRaisesRegex(EngineError, "Load the source"):
            self.engine.commit(self.sid, data)

    def test_relations_are_directional_and_stable(self):
        p, _ = self.packet(participants=["adrian", "silas"])
        present = frame(1)
        present["remote"] = {"silas": "Наблюдает за разговором в отражении"}
        data = self.turn(p, participants=["adrian", "silas"], state=present, relations=[
            {"source": "adrian", "target": "pov",
             "status": "acquainted", "reason": "Лично знакомы"},
            {"source": "adrian", "target": "silas",
             "status": "acquainted", "reason": "Лично знакомы"},
            {"source": "adrian", "target": "pov", "metric": "доверие",
             "value": 20, "reason": "Сомнение"},
            {"source": "adrian", "target": "silas", "metric": None,
             "value": "Злится на Сайласа", "reason": "Конфликт"},
        ])
        self.engine.commit(self.sid, data)
        rel = json.loads(self.path("relationships.md").read_text(encoding="utf-8"))
        self.assertEqual(rel["npc_to_pov"]["adrian"]["доверие"], 20)
        self.assertEqual(rel["npc_to_npc"]["adrian"]["silas"], "Злится на Сайласа")
        self.assertNotIn("silas", rel["npc_to_npc"])

    def test_state_disallows_biography_and_memories(self):
        p, _ = self.packet()
        data = self.turn(p)
        data["state"]["biography"] = "Детство Адриана"
        with self.assertRaisesRegex(EngineError, "strict scene fields"):
            self.engine.commit(self.sid, data)
        data = self.turn(p)
        data["state"]["pov"]["unfinished"] = ["Адриан узнал тайну"]
        with self.assertRaises(EngineError):
            self.engine.commit(self.sid, data)

    def test_state_disallows_unknown_character_or_teleportation_fields(self):
        p, _ = self.packet()
        data = self.turn(p)
        data["state"]["present"]["stranger"] = empty_actor()
        with self.assertRaisesRegex(EngineError, "Unknown or duplicated"):
            self.engine.commit(self.sid, data)

    def test_duplicate_event_and_repeated_knowledge_rejected(self):
        p, _ = self.packet()
        old = self.turn(p, narration="**Адриан** — Старый вопрос.",
                        knowledge=[{"owner": "adrian", "fact": "Слышал вопрос",
                                    "source": "Старый вопрос."}],
                        events=[{"text": "Адриан пообещал вернуться.", "participants": ["adrian"]}])
        self.engine.commit(self.sid, old)
        p2, _ = self.packet("повтор")
        new = self.turn(p2, n=2, reader="повтор",
                        narration="**Адриан** — Другой вопрос.",
                        knowledge=[{"owner": "adrian", "fact": "Слышал вопрос",
                                    "source": "Другой вопрос."}])
        with self.assertRaisesRegex(EngineError, "Fact already recorded"):
            self.engine.commit(self.sid, new)
        duplicate = self.turn(p2, n=2, reader="повтор",
                              events=[{"text": "Адриан пообещал вернуться.",
                                       "participants": ["adrian"]}])
        with self.assertRaisesRegex(EngineError, "Identical world milestone"):
            self.engine.commit(self.sid, duplicate)
        self.assertEqual(len(self.engine.history(self.sid)), 1)

    def _prepare_real_audit_with_one_saved_fact(self):
        for n in range(1, 16):
            text = "реальный ход " + str(n)
            p, _ = self.packet(text)
            knowledge = ([{"owner": "adrian", "fact": "Узнал важный факт",
                           "source": "Я услышал это."}] if n == 1 else [])
            events = ([{"text": "Началась важная встреча.",
                        "participants": ["pov", "adrian"]}] if n == 1 else [])
            self.engine.commit(self.sid, self.turn(
                p, n=n, reader=text, knowledge=knowledge, events=events))
        self.assertEqual(self.engine.status(self.sid)["audit_due"], 15)
        p, _ = self.packet(kind="audit")
        report = {key: "Проверено по сценам" for key in
                  ("continuity", "state", "knowledge", "timeline", "relationships")}
        return p, report

    def test_audit_detects_lost_personal_fact_in_real_history(self):
        p, report = self._prepare_real_audit_with_one_saved_fact()
        atomic(self.path("knowledge/adrian.md"), "# Новые знания: adrian\n")
        with self.assertRaisesRegex(EngineError, "previously saved personal fact"):
            self.engine.audit(self.sid, p["version"], p["packet_id"],
                              p["chunk_hashes"], report)
        self.assertEqual(self.engine.status(self.sid)["audit_due"], 15)

    def test_audit_detects_lost_world_event_in_real_history(self):
        p, report = self._prepare_real_audit_with_one_saved_fact()
        atomic(self.path("timeline.md"), "# Хронология\n")
        with self.assertRaisesRegex(EngineError, "previously saved world event"):
            self.engine.audit(self.sid, p["version"], p["packet_id"],
                              p["chunk_hashes"], report)
        self.assertEqual(self.engine.status(self.sid)["audit_due"], 15)

    def test_audit_cannot_erase_knowledge_or_timeline(self):
        data_path = self.path("system/meta.json")
        meta = json.loads(data_path.read_text(encoding="utf-8"))
        meta["turn"], meta["audit_due"] = 15, 15
        atomic(data_path, dump(meta))
        p, _ = self.packet(kind="audit")
        report = {key: "Проверено" for key in
                  ("continuity", "state", "knowledge", "timeline", "relationships")}
        originals = {
            "knowledge/adrian.md": self.path("knowledge/adrian.md").read_text(encoding="utf-8"),
            "timeline.md": self.path("timeline.md").read_text(encoding="utf-8"),
        }
        for name, old in originals.items():
            with self.subTest(name=name), self.assertRaisesRegex(EngineError, "cannot erase"):
                self.engine.audit(
                    self.sid, p["version"], p["packet_id"], p["chunk_hashes"], report,
                    {name: {"before_sha256": digest(old), "after": "# Подменили старый текст",
                            "reason": "Стереть историю"}})

    def test_audit_can_append_correction_with_fulldiff_archive(self):
        first_packet, _ = self.packet()
        self.engine.commit(self.sid, self.turn(
            first_packet, narration="**Адриан** — Я услышал это."))
        data_path = self.path("system/meta.json")
        meta = json.loads(data_path.read_text(encoding="utf-8"))
        meta["turn"], meta["audit_due"] = 15, 15
        atomic(data_path, dump(meta))
        p, _ = self.packet(kind="audit")
        old = self.path("knowledge/adrian.md").read_text(encoding="utf-8")
        new = old + "\nУточнение, подтверждённое исходной сценой. [источник: Я услышал это.]\n"
        report = {key: "Проверено" for key in
                  ("continuity", "state", "knowledge", "timeline", "relationships")}
        result = self.engine.audit(
            self.sid, p["version"], p["packet_id"], p["chunk_hashes"], report,
            {"knowledge/adrian.md": {"before_sha256": digest(old), "after": new,
                                      "reason": "Отмечено пропущенное знание",
                                      "evidence": "Я услышал это."}})
        self.assertEqual(result["corrections"], 1)
        self.assertEqual(self.path("knowledge/adrian.md").read_text(encoding="utf-8"), new)
        audit_record = json.loads(self.path("system/audit.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(audit_record["corrections"][0]["before"], old)
        self.assertEqual(audit_record["corrections"][0]["after"], new)
        self.assertEqual(self.engine.status(self.sid)["audit_due"], 0)

    def test_rollback_restores_documents_even_after_restart(self):
        p, _ = self.packet()
        data = self.turn(p, knowledge=[{
            "owner": "adrian", "fact": "Услышал фразу", "source": "Я услышал это."
        }], events=[{"text": "Адриан рассказал о разговоре.", "participants": ["adrian"]}])
        saved = self.engine.commit(self.sid, data)
        restarted = Engine(self.root / "storage", self.root / "instructions")
        restarted.rollback(self.sid, saved["version"], data["request_id"], "Откат")
        self.assertEqual(restarted.history(self.sid), [])
        self.assertEqual(self.path("timeline.md").read_text(encoding="utf-8"), "# Хронология\n")
        self.assertEqual(self.path("knowledge/adrian.md").read_text(encoding="utf-8"),
                         "# Новые знания: adrian\n")
        self.assertEqual(json.loads(self.path("state.md").read_text(encoding="utf-8"))["turn"], 0)
        self.assertIn('"kind":"rollback"', self.path("history.jsonl").read_text(encoding="utf-8"))

    def test_crash_mid_transaction_restores_every_file_together(self):
        meta_path = self.path("system/meta.json")
        before = json.loads(meta_path.read_text(encoding="utf-8"))
        next_meta = {**before, "version": before["version"] + 1}
        after_state = dump(frame(1))
        after_timeline = "# Хронология\n\nДобавлена в одной транзакции.\n"
        files = {
            "state.md": base64.b64encode(after_state.encode()).decode(),
            "timeline.md": base64.b64encode(after_timeline.encode()).decode(),
        }
        # Simulate crash after first file was updated but before commit metadata.
        atomic(meta_path, dump({**before, "pending": {"files": files, "next": next_meta}}))
        atomic(self.path("state.md"), after_state)
        self.assertNotEqual(self.path("timeline.md").read_text(encoding="utf-8"),
                            after_timeline)
        restarted = Engine(self.root / "storage", self.root / "instructions")
        self.assertEqual(restarted.status(self.sid)["version"], next_meta["version"])
        self.assertEqual(self.path("timeline.md").read_text(encoding="utf-8"),
                         after_timeline)
        self.assertEqual(json.loads(self.path("state.md").read_text(encoding="utf-8"))["turn"], 1)

    def test_two_sessions_never_share_character_memory(self):
        other = self.create(self.source, self.blocks)
        meta = self.engine.status(other)
        self.engine.approve(other, meta["version"], meta["canon_hash"], "Утверждаю")
        self.engine.set_opening(other, self.engine.status(other)["version"], frame())
        p, _ = self.packet()
        data = self.turn(p, narration="**Адриан** — Секретная записка.",
                         knowledge=[{"owner": "adrian", "fact": "Узнал о записке",
                                     "source": "Секретная записка."}])
        self.engine.commit(self.sid, data)
        other_path = self.root / "storage" / other / "knowledge" / "adrian.md"
        self.assertNotIn("записке", other_path.read_text(encoding="utf-8"))
        self.assertEqual(self.engine.status(other)["turn"], 0)

    def test_new_character_is_optional_and_id_has_no_number(self):
        meta = self.engine.status(self.sid)
        self.engine.add_character(self.sid, meta["version"], "margaret", "Маргарет",
                                  "Маргарет вошла в комнату.")
        self.assertTrue(self.path("characters/margaret.md").exists())
        p, v = self.packet(participants=["adrian", "margaret"])
        self.assertIn("characters/margaret.md", v["files"])
        self.assertEqual(v["files"]["knowledge/margaret.md"], "# Новые знания: margaret\n")
        self.assertNotIn("NPC_001", v["files"]["novel.md"])
        self.assertGreaterEqual(p["chunk_count"], 1)


if __name__ == "__main__":
    unittest.main()
