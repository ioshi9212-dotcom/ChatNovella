"""Real JSON-RPC tests against temporary V2 data. Never touch Lite or /data."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import novella_v2.api as api
from novella_v2.tests.test_story_hooks import fixture_plan
from novella_v2.engine import Engine, empty_actor


TOOLS = {
    "load_v2_instructions", "get_v2_instruction_versions", "create_v2_novel",
    "get_v2_status", "approve_v2_canon", "set_v2_opening", "add_v2_character",
    "get_v2_director_brief", "backfill_v2_hooks",
    "get_v2_review_manifest",
    "prepare_v2_context", "read_v2_chunk", "review_v2_scene",
    "save_v2_turn", "finish_v2_audit",
    "search_v2_history", "rollback_v2_turn",
}


def digest(text):
    return sha256(text.encode("utf-8")).hexdigest()


class TestV2MCP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(api.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.engine = Engine(self.data)
        self.store_patch = patch.object(api, "store", self.engine)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.headers = {"Accept": "application/json, text/event-stream"}
        self.serial = 0

    def rpc(self, method, params=None, *, headers=None):
        self.serial += 1
        response = self.client.post("/mcp", headers=headers or self.headers, json={
            "jsonrpc": "2.0", "id": self.serial, "method": method, "params": params or {},
        })
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertNotIn("error", value, value)
        return value["result"]

    def tool(self, tool_name, **arguments):
        if tool_name == "prepare_v2_context" and arguments.get("kind") == "turn":
            brief = self.tool("get_v2_director_brief", session_id=arguments["session_id"])
            arguments.setdefault("story_plan", fixture_plan(brief))
        result = self.rpc("tools/call", {"name": tool_name, "arguments": arguments})
        self.assertFalse(result.get("isError"), result)
        return result["structuredContent"]

    @staticmethod
    def state(turn):
        return {"turn": turn, "datetime": None, "location": "Тестовая библиотека",
                "pov": empty_actor(), "present": {"adrian": empty_actor()},
                "remote": {}, "objects": ["красная книга на столе"], "unfinished": []}

    def create(self, long=False):
        original = ("Мир: закрытая библиотека. Пропал ключ от шкафа.\n" +
                    ("Описание тестовой библиотеки. " * 1600 if long else "") +
                    "\nPOV: тестовая героиня.\nАдриан: тестовый библиотекарь.\n"
                    "Сайлас: отсутствует и не слышит разговор.\n")
        return self.tool("create_v2_novel", original=original, blocks=[
            {"anchor": "Мир:", "target": "novel"},
            {"anchor": "POV:", "target": "pov"},
            {"anchor": "Адриан:", "target": "adrian"},
            {"anchor": "Сайлас:", "target": "silas"},
        ], names={"pov": "Героиня", "adrian": "Адриан", "silas": "Сайлас"},
            relationships={"familiarity": {
                "adrian": {"pov": "acquainted"}, "pov": {"adrian": "acquainted"}},
                "npc_to_pov": {"adrian": {"доверие": 40}}, "npc_to_npc": {}},
            initial_threads=[{"id": "missing_key", "title": "Пропавший ключ",
                              "status": "not_started", "note": "Ключ ещё не найден",
                              "source": "Пропал ключ от шкафа."}])

    def test_malformed_legacy_story_memory_does_not_reject_creation(self):
        original = "Мир: тест.\n"
        created = self.tool("create_v2_novel", original=original,
                            blocks=[{"anchor": "Мир:", "target": "novel"}],
                            names={}, initial_threads="bad",
                            initial_hooks=[{"id": "incomplete"}])
        sid = created["session_id"]
        self.assertEqual({w["field"] for w in created["story_memory_warnings"]},
                         {"initial_threads", "initial_hooks"})
        self.assertEqual((self.data / sid / "original.txt").read_text(
            encoding="utf-8"), original)
        self.assertFalse(created["approved"])

    def read(self, sid, packet):
        parts = []
        for index in packet["unread_chunk_indices"]:
            chunk = self.tool("read_v2_chunk", session_id=sid,
                              packet_id=packet["packet_id"], index=index)
            self.assertLessEqual(len(chunk["content"]), 36000)
            self.assertEqual(digest(chunk["content"]), packet["chunk_hashes"][index])
            self.assertEqual(chunk["sha256"], packet["chunk_hashes"][index])
            parts.append(chunk["content"])
        self.assertTrue(chunk["all_chunks_read"])
        content = "".join(parts)
        self.assertEqual(digest(content), packet["sha256"])
        files = json.loads(content)["files"]
        self.assertIn("relationships.md", files)
        self.assertIn("open_threads.md", files)
        self.assertNotIn("engine/rules.md", files)
        self.assertNotIn("engine/skin_builder.md", files)
        return files

    def start(self, long=False):
        created = self.create(long)
        sid = created["session_id"]
        self.assertTrue(sid.startswith("NV2_"))
        canon = self.tool("prepare_v2_context", session_id=sid, reader_text="",
                          participants=[], kind="canon", request_id="canon-preview")
        if long:
            self.assertGreater(canon["chunk_count"], 1)
        files = self.read(sid, canon)
        self.assertEqual(files["original.txt"], (self.data / sid / "original.txt").read_text())
        approved = self.tool("approve_v2_canon", session_id=sid,
                             expected_version=created["version"], canon_hash=created["canon_hash"],
                             confirmation="Утверждаю полную синтетическую тестовую анкету")
        self.tool("set_v2_opening", session_id=sid, expected_version=approved["version"],
                  state=self.state(0))
        return sid

    def body(self, packet, number=1):
        scene = ("Героиня наклонилась к Адриану и прошептала: «Ключ спрятан в красной книге». "
                 "Адриан услышал её, протянул ей свой ключ и сказал: «Я доверяю тебе».")
        return {"request_id": "mcp-turn-" + str(number), "expected_version": packet["version"],
                "packet_id": packet["packet_id"], "chunk_hashes": packet["chunk_hashes"],
                "reader_text": "Шепчу Адриану, где спрятан ключ.", "narration": scene,
                "participants": ["adrian"], "state": self.state(number),
                "knowledge": [{"owner": "adrian", "fact": "Ключ спрятан в красной книге",
                               "source": "Ключ спрятан в красной книге"}],
                "events": [{"text": "Героиня сообщила Адриану тайник ключа.",
                            "participants": ["pov", "adrian"]}],
                "relations": [{"source": "adrian", "target": "pov", "metric": "доверие",
                               "value": 42, "reason": "Доверился после разговора",
                               "evidence": "Я доверяю тебе"}],
                "thread_updates": [{"id": "missing_key", "title": "Пропавший ключ",
                                    "status": "active", "note": "Обнаружен тайник ключа",
                                    "source": "Ключ спрятан в красной книге"}]}

    def test_public_transport_and_all_typed_tools(self):
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        public = self.client.post("/mcp", json=message, headers=self.headers)
        self.assertEqual(public.status_code, 200, public.text)
        self.assertIn("tools", public.json()["result"])
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "V2 acceptance", "version": "1.0"}})
        self.assertEqual(init["serverInfo"]["name"], "ChatNovella V2")
        self.assertIn("rules_only=true", init["instructions"])
        tools = self.rpc("tools/list")["tools"]
        self.assertEqual({t["name"] for t in tools}, TOOLS)
        turn = next(t for t in tools if t["name"] == "save_v2_turn")["inputSchema"]
        self.assertIn("State", turn["$defs"])
        self.assertIn("Knowledge", turn["$defs"])
        self.assertIn("ThreadUpdate", turn["$defs"])
        self.assertEqual(set(turn["$defs"]["State"]["required"]), set(self.state(0)))
        denied = self.rpc("tools/call", {"name": "get_v2_status", "arguments": {
            "session_id": "LITE_78525c917cca4417a8723dca965813a0"}})
        self.assertTrue(denied["isError"])
        hostile = self.client.post("/mcp", json=message, headers={
            **self.headers, "Host": "untrusted.example"})
        self.assertEqual(hostile.status_code, 421)

    def test_director_and_evidence_only_hook_backfill_via_mcp(self):
        sid = self.start()
        original = self.tool("get_v2_director_brief", session_id=sid)
        self.assertTrue(original["empty_ledger"])
        self.assertIn("open_arcs", original)
        self.assertIn("cast", original)
        version = self.tool("get_v2_status", session_id=sid)["version"]
        migrated = self.tool("backfill_v2_hooks", session_id=sid,
                             expected_version=version, additions=[{
                                 "id": "missing_key_hook", "arc_id": "missing_key",
                                 "title": "Пропавший ключ", "kind": "mystery",
                                 "status": "open",
                                 "detail": "Неизвестно, кто забрал ключ из шкафа",
                                 "participants": ["adrian"],
                                 "source": "Пропал ключ от шкафа.",
                                 "origin_turn": 0,
                             }])
        self.assertTrue(migrated["updated"])
        brief = self.tool("get_v2_director_brief", session_id=sid)
        self.assertEqual(brief["unresolved_hooks"][0]["id"], "missing_key_hook")
        self.assertFalse(brief["empty_ledger"])
        packet = self.tool("prepare_v2_context", session_id=sid,
                           reader_text="Смотрю на шкаф",
                           participants=["adrian"], kind="turn",
                           request_id="mcp-hook-inspection")
        files = self.read(sid, packet)
        self.assertIn("hooks.md", files)
        self.assertIn("missing_key_hook", files["hooks.md"])

    def test_instructions_hashes_and_author_skin_unchanged(self):
        bootstrap = self.tool("load_v2_instructions", rules_only=True)
        self.assertNotIn("skin_builder", bootstrap)
        instructions = self.tool("load_v2_instructions")
        for name in ("rules", "skin_builder"):
            self.assertEqual(instructions[name]["sha256"], digest(instructions[name]["content"]))
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(instructions["skin_builder"]["content"], (root / "skin_builder.md").read_bytes().decode("utf-8"))
        versions = self.tool("get_v2_instruction_versions")
        self.assertEqual(versions["rules_sha256"], instructions["rules"]["sha256"])
        self.assertNotIn("content", json.dumps(versions))

    def test_default_mcp_turn_skips_literary_review_with_real_builder(self):
        sid = self.start()
        p = self.tool("prepare_v2_context", session_id=sid,
                      reader_text="Привет", participants=["adrian"],
                      kind="turn", request_id="no-literary-review")
        self.assertFalse(p["scene_review_enabled"])
        self.read(sid, p)
        turn = {"request_id": "no-literary-review", "expected_version": p["version"],
                "packet_id": p["packet_id"], "chunk_hashes": p["chunk_hashes"],
                "reader_text": "Привет", "narration": "Адриан поздоровался.",
                "participants": ["adrian"], "state": self.state(1),
                "knowledge": [], "events": [], "relations": []}
        result = self.tool("save_v2_turn", session_id=sid, turn=turn)
        self.assertTrue(result["saved"])
        self.assertEqual(result["turn"], 1)

    def test_review_tool_returns_full_builder_failures_before_save(self):
        self.engine.scene_review_enabled = True
        sid = self.start()
        p = self.tool("prepare_v2_context", session_id=sid,
                      reader_text="Оглядываюсь", participants=["adrian"],
                      kind="turn", request_id="preflight-mcp")
        self.read(sid, p)
        instructions = self.tool("load_v2_instructions")
        reviewed = self.tool(
            "review_v2_scene", session_id=sid,
            expected_version=p["version"], packet_id=p["packet_id"],
            request_id="preflight-mcp",
            skin_sha256=instructions["skin_builder"]["sha256"],
            narration="Один абзац без оформления.", checks=[])
        self.assertFalse(reviewed["approved"])
        self.assertTrue(any("Не проверен раздел" in item for item in reviewed["issues"]))
        self.assertTrue(any("Шапка" in item for item in reviewed["issues"]))
        self.assertTrue(any("Не проверено правило" in item for item in reviewed["issues"]))
        manifest = self.tool("get_v2_review_manifest")
        self.assertGreater(len(manifest["rules"]), 50)
        self.assertEqual(manifest["skin_sha256"], instructions["skin_builder"]["sha256"])
        self.assertEqual(self.tool("get_v2_status", session_id=sid)["turn"], 0)

    def use_empty_synthetic_skin_for_non_literary_storage_tests(self):
        """Legacy transaction/audit fixtures use one-sentence fake scenes.
        The real authored builder is exercised by test_scene_preflight.py.
        """
        root = Path(self.tmp.name) / "empty-instructions"
        root.mkdir()
        (root / "rules.md").write_text("Storage-only synthetic tests", encoding="utf-8")
        (root / "skin_builder.md").write_text("", encoding="utf-8")
        self.engine.instructions = root

    def test_end_to_end_chunks_memory_arcs_duplicate_restart_and_rollback(self):
        self.use_empty_synthetic_skin_for_non_literary_storage_tests()
        sid = self.start(long=True)
        prepared = self.tool("prepare_v2_context", session_id=sid,
                             reader_text="Шепчу Адриану, где спрятан ключ.",
                             participants=["adrian"], kind="turn", request_id="mcp-turn-1")
        self.read(sid, prepared)
        turn = self.body(prepared)
        wrong = deepcopy(turn)
        wrong["knowledge"][0]["owner"] = "silas"
        rejected = self.rpc("tools/call", {"name": "save_v2_turn", "arguments": {
            "session_id": sid, "turn": wrong}})
        self.assertTrue(rejected["isError"])
        self.assertIn("KNOWLEDGE_OWNER", rejected["content"][0]["text"])
        self.assertEqual(self.engine.status(sid)["turn"], 0)
        saved = self.tool("save_v2_turn", session_id=sid, turn=turn)
        self.assertTrue(saved["saved"])
        self.assertEqual(saved, self.tool("save_v2_turn", session_id=sid, turn=turn))
        replay = self.tool("prepare_v2_context", session_id=sid,
                           reader_text=turn["reader_text"], participants=["adrian"],
                           kind="turn", request_id="mcp-turn-1")
        self.assertTrue(replay["already_committed_duplicate"])
        self.assertEqual(replay["narration"], saved["narration"])
        history = self.tool("search_v2_history", session_id=sid, query="красной книге")
        self.assertEqual(len(history["scenes"]), 1)
        # Reopening the same temporary storage is local persistence validation,
        # not a claim that an Amvera project has been restarted.
        api.store = Engine(self.data)
        status = self.tool("get_v2_status", session_id=sid)
        self.assertEqual(status["turn"], 1)
        next_packet = self.tool("prepare_v2_context", session_id=sid, reader_text="Проверка памяти",
                                participants=["adrian", "silas"], kind="turn", request_id="mcp-inspect")
        files = self.read(sid, next_packet)
        self.assertIn("Ключ спрятан в красной книге", files["knowledge/adrian.md"])
        self.assertNotIn("Ключ спрятан в красной книге", files["knowledge/silas.md"])
        self.assertIn("тайник", files["open_threads.md"])
        self.assertEqual(json.loads(files["relationships.md"])["npc_to_pov"]["adrian"]["доверие"], 42)
        undone = self.tool("rollback_v2_turn", session_id=sid, expected_version=status["version"],
                           request_id="mcp-turn-1", reason="Откат синтетической тестовой сцены")
        self.assertEqual(undone["turn"], 0)
        self.assertEqual(self.tool("search_v2_history", session_id=sid)["scenes"], [])
        self.assertNotIn("Ключ спрятан в красной книге", (self.data / sid / "knowledge/adrian.md").read_text())
        self.assertIn('"kind":"rollback"', (self.data / sid / "history.jsonl").read_text())
        self.assertEqual(json.loads((self.data / sid / "relationships.md").read_text())["npc_to_pov"]["adrian"]["доверие"], 40)

    def test_add_character_and_finish_required_audit(self):
        self.use_empty_synthetic_skin_for_non_literary_storage_tests()
        sid = self.start()
        status = self.engine.status(sid)
        added = self.tool("add_v2_character", session_id=sid, expected_version=status["version"],
                          name="marcus", display="Маркус", established_text="Маркус вошёл и назвался.")
        self.assertEqual(added["version"], status["version"])
        self.assertFalse(added["added"])
        self.assertFalse((self.data / sid / "characters/marcus.md").exists())
        for number in range(1, 16):
            brief = self.engine.director_brief(sid)
            people = ["adrian", "marcus"] if number == 1 else ["adrian"]
            packet = self.engine.prepare(sid, str(number), people, "turn", f"audit-seed-{number}",
                                         story_plan=fixture_plan(brief),
                                         new_characters=[added["proposal"]] if number == 1 else None)
            for index in packet["unread_chunk_indices"]:
                self.engine.read_chunk(sid, packet["packet_id"], index)
            narration = f"Адриан перелистнул страницу {number}."
            state = self.state(number)
            if number == 1:
                narration += " Маркус вошёл и назвался."
                state["present"]["marcus"] = empty_actor()
            self.engine.commit(sid, {
                "request_id": f"audit-seed-{number}", "expected_version": packet["version"],
                "packet_id": packet["packet_id"], "chunk_hashes": packet["chunk_hashes"],
                "reader_text": str(number), "narration": narration,
                "participants": people, "state": state,
                "knowledge": [], "events": [], "relations": [],
            })
        blocked = self.rpc("tools/call", {"name": "prepare_v2_context", "arguments": {
            "session_id": sid, "reader_text": "Дальше", "participants": ["adrian"],
            "kind": "turn", "request_id": "after-audit"}})
        self.assertTrue(blocked["isError"])
        self.assertIn("AUDIT_GATE", blocked["content"][0]["text"])
        packet = self.tool("prepare_v2_context", session_id=sid, reader_text="",
                           participants=[], kind="audit", request_id="audit-15")
        self.read(sid, packet)
        finished = self.tool("finish_v2_audit", session_id=sid, expected_version=packet["version"],
                             packet_id=packet["packet_id"], chunk_hashes=packet["chunk_hashes"],
                             report={**{key: "Сверено по полным сценам" for key in (
                                 "continuity", "state", "knowledge", "timeline", "relationships")},
                                 "story": {"arc:missing_key": "Тайна ключа остаётся открытой."}},
                             corrections={})
        self.assertTrue(finished["audited"])
        self.assertEqual(self.tool("get_v2_status", session_id=sid)["audit_due"], 0)


if __name__ == "__main__":
    unittest.main()
