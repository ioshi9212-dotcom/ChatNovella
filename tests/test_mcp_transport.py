"""Exercise the real HTTP transport with the public reverse-proxy hostname."""
import importlib
import os
import tempfile
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient


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
            self.assertEqual(len(tools), 12)
            self.assertTrue({"prepare_novella_turn", "read_novella_chunk",
                             "save_novella_turn"} <= tools)
            response = self.post(client, "tools/call", {
                "name": "list_novella_sessions", "arguments": {},
            })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["result"].get("isError", False))

    def test_unknown_host_still_rejected(self):
        with TestClient(self.server.app, base_url="https://untrusted.example") as client:
            response = self.post(client, "initialize")
            self.assertEqual(response.status_code, 421)

    def test_chatgpt_origin_allowed_unknown_origin_rejected(self):
        with TestClient(self.server.app, base_url=f"https://{self.server.PUBLIC_HOST}:443") as client:
            response = self.post(client, "tools/list", headers={"Origin": "https://chatgpt.com"})
            self.assertEqual(response.status_code, 200, response.text)
            response = self.post(client, "tools/list", headers={"Origin": "https://untrusted.example"})
            self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
