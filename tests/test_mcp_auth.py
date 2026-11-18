"""HTTP access tests: tokenless mode and optional Bearer protection."""
import asyncio
import importlib
import os
import tempfile
import unittest
from unittest.mock import patch


class TestOptionalBearer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {
            "CHATNOVELLA_API_TOKEN": "",
            "CHATNOVELLA_DATA_DIR": cls.temp.name,
        }):
            cls.server = importlib.import_module("chatnovella.mcp_server")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def call_app(self, token, path="/mcp", headers=None):
        sent = []
        async def dummy_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"reached"})
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            sent.append(message)
        scope = {"type": "http", "path": path, "method": "GET", "headers": headers or []}
        app = self.server.BearerMiddleware(dummy_app, token)
        asyncio.run(app(scope, receive, send))
        return next(m["status"] for m in sent if m["type"] == "http.response.start")

    def test_no_token_allows_request(self):
        self.assertEqual(self.call_app(""), 200)

    def test_configured_token_rejects_request_without_bearer(self):
        self.assertEqual(self.call_app("test-token-longer-than-24-chars"), 401)

    def test_configured_token_accepts_correct_bearer(self):
        token = "test-token-longer-than-24-chars"
        self.assertEqual(self.call_app(token, headers=[(b"authorization", b"Bearer " + token.encode())]), 200)

    def test_health_public_in_either_mode(self):
        self.assertEqual(self.call_app("test-token-longer-than-24-chars", path="/health"), 200)


if __name__ == "__main__":
    unittest.main()
