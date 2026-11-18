"""Exercise the real HTTP transport with the public reverse-proxy hostname."""
import importlib
import os
import tempfile
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
from review_helpers import checks, scene


class TestMCPTransport(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict(os.environ, {
            "CHATNOVELLA_API_TOKEN": "",
            "CHATNOVELLA_DATA_DIR": self.temp.name,
            "CHATNOVELLA_PUBLIC_HOST": "chatnovella-yumikofv.waw0.amvera.tech",
        }):
            from chatnovella import mcp_server
            self.server = importlib.reload(mcp_server)

    def post(self, client, method, params=None, headers=None):
        return client.post("/mcp", headers={
            "Accept": "application/json, text/event-stream", **(headers or {}),
        }, json={"jsonrpc": "2.0", "id": 1, "method": method,
                 "params": params or {}})

    def test_public_host_initializes_lists_and_calls_read_only_tool(self):
        with TestClient(self.server.app, base_url=f"https://{self.server.PUBLIC_HOST}") as client:
            response = self.post(client, "initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "transport-test", "version": "1"},
            })
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()["result"]
            self.assertEqual(result["serverInfo"]["name"], "ChatNovella")
            self.assertIn("prepare_novella_turn", result["instructions"])
            self.assertIn("Skin Builder", result["instructions"])
            response = self.post(client, "tools/list")
            self.assertEqual(response.status_code, 200, response.text)
            tools = {tool["name"] for tool in response.json()["result"]["tools"]}
            self.assertEqual(len(tools), 15)
            self.assertTrue({"prepare_novella_turn", "read_novella_chunk",
                             "save_novella_turn", "repair_novella_timeline",
                             "initialize_novella_relationship_metrics", "review_novella_scene"} <= tools)
            response = self.post(client, "tools/call", {
                "name": "list_novella_sessions", "arguments": {},
            })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["result"].get("isError", False))

    def test_unknown_host_still_rejected(self):
        with TestClient(self.server.app, base_url="https://untrusted.example") as client:
            response = self.post(client, "initialize")
            self.assertEqual(response.status_code, 421)

    def test_http_review_and_exact_approved_save(self):
        meta = self.server.store.create_session("Тестовый оригинал", "Канон", "Рината")
        meta = self.server.store.activate(meta["session_id"], meta["version"], True)
        with TestClient(self.server.app, base_url=f"https://{self.server.PUBLIC_HOST}") as client:
            def call(name, arguments):
                response = self.post(client, "tools/call", {"name": name, "arguments": arguments})
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()["result"]
                self.assertFalse(result.get("isError", False), result)
                return result["structuredContent"]

            packet = call("prepare_novella_turn", {"session_id": meta["session_id"], "participants": []})
            self.assertIn("engine/scene_review.md", packet["engine_files"])
            for index in range(packet["count"]):
                call("read_novella_chunk", {"packet_id": packet["packet_id"], "index": index})
            payload = {
                "session_id": meta["session_id"], "expected_version": meta["version"],
                "packet_id": packet["packet_id"], "reader_text": "(Оглядеться)",
                "narration": scene(), "files": {"state/scene.md": "Рината осмотрела зал."},
                "timeline_events": [], "timeline_no_change_reason": "Нет новых значимых сюжетных фактов",
            }
            reviewed = call("review_novella_scene", {**payload, "checks": checks(), "relationship_labels": {}})
            self.assertTrue(reviewed["approved"], reviewed)
            saved = call("save_novella_turn", {**payload, "review_id": reviewed["review_id"]})
            self.assertEqual(saved["turn"], 1)

    def test_chatgpt_origin_allowed_unknown_origin_rejected(self):
        with TestClient(self.server.app, base_url=f"https://{self.server.PUBLIC_HOST}:443") as client:
            response = self.post(client, "tools/list", headers={"Origin": "https://chatgpt.com"})
            self.assertEqual(response.status_code, 200, response.text)
            response = self.post(client, "tools/list", headers={"Origin": "https://untrusted.example"})
            self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
