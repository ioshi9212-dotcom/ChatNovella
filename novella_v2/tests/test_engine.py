"""Deterministic V2 store tests; never read or modify production /data."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novella_v2.engine import CHUNK_SIZE, Engine, EngineError, digest, empty_actor


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        instructions = self.root / "instructions"
        instructions.mkdir()
        (instructions / "rules.md").write_text("Технические правила", encoding="utf-8")
        (instructions / "skin_builder.md").write_text("Скинбилдер без правок", encoding="utf-8")
        self.engine = Engine(self.root / "storage", instructions)
        self.original = "Мир жестокий.\nАдриан упрямый и грубый.\nСайлас умный.\n"
        a = self.original.index("Адриан")
        b = self.original.index("Сайлас")
        self.blocks = [
            {"start": 0, "end": a, "target": "novel"},
            {"start": a, "end": b, "target": "adrian"},
            {"start": b, "end": len(self.original), "target": "silas"},
        ]
        self.sid = self.engine.create(
            self.original, self.blocks, {"adrian": "Адриан", "silas": "Сайлас"},
            {"хоррор": "высокий", "романтика": "умеренная"})["session_id"]
        status = self.engine.status(self.sid)
        self.engine.approve(self.sid, status["version"], status["canon_hash"], "Подтверждаю")
        self.engine.set_opening(self.sid, self.engine.status(self.sid)["version"],
                                self.state(0))

    @staticmethod
    def state(turn):
        return {"turn": turn, "datetime": "2026-09-12T18:00:00",
                "location": "Кухня",
                "pov": empty_actor(),
                "present": {"adrian": empty_actor()}, "remote": {},
                "objects": ["телефон на столе"], "unfinished": []}

    def packet(self, text="Привет", participants=None, kind="turn"):
        packet = self.engine.prepare(self.sid, text, participants or ["adrian"], kind)
        content = []
        for index in range(packet["chunk_count"]):
            part = self.engine.read_chunk(self.sid, packet["packet_id"], index)
            self.assertLessEqual(len(part["content"]), CHUNK_SIZE)
            self.assertEqual(part["sha256"], packet["chunk_hashes"][index])
            content.append(part["content"])
        self.assertEqual(digest("".join(content)), packet["sha256"])
        return packet, json.loads("".join(content))

    def turn(self, number=1, text="Привет", narration="**Адриан** — Ты уезжаешь завтра?",
             knowledge=None, events=None, relations=None, packet=None):
        packet = packet or self.packet(text)[0]
        return {"request_id": "req-" + str(number), "expected_version": packet["version"],
                "packet_id": packet["packet_id"], "chunk_hashes": packet["chunk_hashes"],
                "reader_text": text, "narration": narration,
                "participants": ["adrian"], "state": self.state(number),
                "knowledge": knowledge or [], "events": events or [], "relations": relations or []}

    def test_original_is_exact_and_cards_are_not_paraphrased(self):
        directory = self.root / "storage" / self.sid
        self.assertEqual((directory / "original.txt").read_text(encoding="utf-8"),
                         self.original)
        adrian = (directory / "characters/adrian.md").read_text(encoding="utf-8")
        self.assertTrue(adrian.endswith("Адриан упрямый и грубый.\n"))
        self.assertIn("хоррор: высокий", (directory / "novel.md").read_text(encoding="utf-8"))
        self.assertFalse((directory / "characters/pov.md").exists())

    def test_gap_or_overlap_rejected(self):
        bad = deepcopy(self.blocks)
        bad[1]["start"] += 1
        with self.assertRaisesRegex(EngineError, "Blocks must exactly partition"):
            self.engine.create(self.original, bad, {"adrian": "Адриан", "silas": "Сайлас"})

    def test_chunk_size_is_unicode_not_bytes(self):
        # Every Russian character occupies more than one UTF-8 byte.
        directory = self.root / "storage" / self.sid
        long_novel = (directory / "novel.md").read_text(encoding="utf-8") + "Ж" * 90_000
        (directory / "novel.md").write_text(long_novel, encoding="utf-8")
        packet, payload = self.packet()
        self.assertGreaterEqual(packet["chunk_count"], 3)
        self.assertEqual(len(payload["files"]["novel.md"]), len(long_novel))
        self.assertEqual(payload["files"]["novel.md"], long_novel)
        self.assertNotIn("characters/silas.md", payload["files"])

    def test_unread_chunks_block_commit(self):
        packet = self.engine.prepare(self.sid, "Привет", ["adrian"])
        data = self.turn(packet=packet)
        with self.assertRaisesRegex(EngineError, "Read and verify all"):
            self.engine.commit(self.sid, data)

    def test_commit_idempotent_and_searchable_history(self):
        packet, payload = self.packet()
        self.assertIn("engine/skin_builder.md", payload["files"])
        data = self.turn(packet=packet, knowledge=[
            {"owner": "adrian", "fact": "POV собирается уехать",
             "source": "Ты уезжаешь завтра?"}],
            events=[{"text": "Адриан спросил героиню об отъезде.",
                     "participants": ["adrian", "pov"]}],
            relations=[{"source": "adrian", "target": "pov",
                        "metric": "доверие", "value": 52, "reason": "Разговор"}])
        first = self.engine.commit(self.sid, data)
        self.assertEqual(first, self.engine.commit(self.sid, data))
        rows = self.engine.history(self.sid, query="уезжаешь")
        self.assertEqual(len(rows), 1)
        directory = self.root / "storage" / self.sid
        memory = (directory / "knowledge/adrian.md").read_text(encoding="utf-8")
        self.assertIn("POV собирается уехать", memory)
        self.assertIn("Адриан спросил", (directory / "timeline.md").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((directory / "relationships.md").read_text(
            encoding="utf-8"))["npc_to_pov"]["adrian"]["доверие"], 52)

    def test_rollback_restores_all_documents_but_keeps_archive(self):
        packet, _ = self.packet()
        data = self.turn(packet=packet, knowledge=[
            {"owner": "adrian", "fact": "Сказано об отъезде", "source": "Ты уезжаешь завтра?"}],
            events=[{"text": "Важный разговор.", "participants": ["pov", "adrian"]}])
        saved = self.engine.commit(self.sid, data)
        undone = self.engine.rollback(self.sid, saved["version"], data["request_id"], "Не тот ход")
        self.assertEqual(undone["turn"], 0)
        self.assertEqual(self.engine.history(self.sid), [])
        directory = self.root / "storage" / self.sid
        self.assertNotIn("Сказано об отъезде", (directory / "knowledge/adrian.md").read_text(encoding="utf-8"))
        self.assertNotIn("Важный разговор.", (directory / "timeline.md").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((directory / "state.md").read_text(encoding="utf-8"))["turn"], 0)
        self.assertIn('"kind":"rollback"', (directory / "history.jsonl").read_text(encoding="utf-8"))
        with self.assertRaises(EngineError):
            self.engine.commit(self.sid, data)

    def test_state_cannot_be_used_as_character_memory(self):
        packet, _ = self.packet()
        data = self.turn(packet=packet)
        data["state"]["unfinished"] = ["Адриан узнал секрет героини"]
        with self.assertRaisesRegex(EngineError, "short physical descriptions"):
            self.engine.commit(self.sid, data)

    def test_unloaded_npc_knowledge_rejected(self):
        packet, _ = self.packet()
        data = self.turn(packet=packet, knowledge=[
            {"owner": "silas", "fact": "POV уезжает", "source": "Ты уезжаешь завтра?"}])
        with self.assertRaisesRegex(EngineError, "loaded participant"):
            self.engine.commit(self.sid, data)

    def test_source_must_exist_in_scene(self):
        packet, _ = self.packet()
        data = self.turn(packet=packet, knowledge=[
            {"owner": "adrian", "fact": "Узнал секрет", "source": "Этой фразы в сцене нет"}])
        with self.assertRaisesRegex(EngineError, "exact excerpt"):
            self.engine.commit(self.sid, data)

    def test_new_character_requires_reprepare(self):
        st = self.engine.status(self.sid)
        self.engine.add_character(self.sid, st["version"], "marcus", "Маркус",
                                  "Вошёл в комнату и назвался Маркусом.")
        packet, payload = self.packet()
        self.assertNotIn("characters/marcus.md", payload["files"])
        new_packet, new_payload = self.packet(participants=["adrian", "marcus"])
        self.assertIn("characters/marcus.md", new_payload["files"])
        data = self.turn(packet=packet)
        data["state"]["present"]["marcus"] = empty_actor()
        data["participants"].append("marcus")
        with self.assertRaisesRegex(EngineError, "Load any entering character"):
            self.engine.commit(self.sid, data)
        self.assertGreater(new_packet["version"], 0)

    def test_due_15_blocks_and_audit_reopens(self):
        for number in range(1, 16):
            packet, _ = self.packet(text="ход " + str(number))
            data = self.turn(number=number, text="ход " + str(number),
                             narration="**Адриан** — Слушаю.", packet=packet)
            saved = self.engine.commit(self.sid, data)
        self.assertEqual(saved["audit_due"], 15)
        with self.assertRaisesRegex(EngineError, "Audit is required"):
            self.engine.prepare(self.sid, "ещё ход", ["adrian"])
        packet, payload = self.packet(text="", kind="audit")
        self.assertEqual(payload["audit_due"], 15)
        self.assertEqual(len(payload["recent_scenes"]), 15)
        report = {key: "Сверено по 15 сценам" for key in
                  ("continuity", "state", "knowledge", "timeline", "relationships")}
        result = self.engine.audit(self.sid, packet["version"],
                                   packet["packet_id"], packet["chunk_hashes"], report)
        self.assertEqual(result["turn"], 15)
        self.assertEqual(self.engine.status(self.sid)["audit_due"], 0)
        self.packet(text="ход 16")

    def test_60_audit_reads_every_character_knowledge(self):
        # Check the long-audit scope without writing 60 fsync-heavy scenes.
        directory = self.root / "storage" / self.sid
        meta_path = directory / "system/meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["turn"] = 60
        meta["audit_due"] = 60
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        packet, payload = self.packet(text="", kind="audit")
        self.assertIn("knowledge/silas.md", payload["files"])
        self.assertEqual(packet["audit_due"], 60)

    def test_wrong_name_or_unknown_character_rejected(self):
        with self.assertRaises(EngineError):
            self.engine.add_character(self.sid, self.engine.status(self.sid)["version"],
                                      "../secret", "X", "Y")
        with self.assertRaises(EngineError):
            self.engine.prepare(self.sid, participants=["unknown"])

    def test_audit_corrections_cannot_rewrite_canon(self):
        directory = self.root / "storage" / self.sid
        meta_path = directory / "system/meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["turn"], meta["audit_due"] = 15, 15
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        packet, _ = self.packet(text="", kind="audit")
        report = {key: "Сверено" for key in
                  ("continuity", "state", "knowledge", "timeline", "relationships")}
        with self.assertRaisesRegex(EngineError, "only correct current"):
            self.engine.audit(self.sid, packet["version"], packet["packet_id"],
                              packet["chunk_hashes"], report,
                              {"novel.md": {"before_sha256": "x", "after": "запрещено",
                                            "reason": "тест"}})


if __name__ == "__main__":
    unittest.main()
