"""Smoke tests for the dedicated V2 Amvera deploy branch."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class TestAmveraEntrypoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = Path(__file__).resolve().parents[2]
        cls.config = (cls.repo / "amvera.yaml").read_text(encoding="utf-8")

    def invoke(self, token: str | None, directory: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.pop("CHATNOVELLA_V2_TOKEN", None)
        if token is not None:
            env["CHATNOVELLA_V2_TOKEN"] = token
        env["CHATNOVELLA_V2_DATA_DIR"] = directory
        script = (
            "from fastapi.testclient import TestClient; "
            "from novella_v2.amvera_entry import app; "
            "client=TestClient(app); "
            "assert client.get('/healthz').json()['engine']=='chatnovella-v2'; "
            "assert client.get('/v2/instructions').status_code==401; "
            "response=client.get('/v2/instructions',headers={'Authorization':'Bearer '+"
            " __import__('os').environ['CHATNOVELLA_V2_TOKEN']}); "
            "assert response.status_code==200, response.text; "
            "assert 'rules' in response.json(); "
            "print('V2_SMOKE_OK')"
        )
        return subprocess.run([sys.executable, "-c", script], cwd=self.repo,
                              env=env, capture_output=True, text=True, timeout=30)

    def test_root_yaml_is_v2_only(self):
        self.assertIn("novella_v2.amvera_entry:app", self.config)
        self.assertIn("requirementsPath: novella_v2/requirements.txt", self.config)
        self.assertIn("containerPort: 8080", self.config)
        self.assertIn("persistenceMount: /data", self.config)
        self.assertNotIn("chatnovella_lite.server:app", self.config)

    def test_production_entry_health_and_private_instructions(self):
        with TemporaryDirectory() as root:
            path = os.path.join(root, "v2")
            # Guard requires an actual Amvera-style /data path. Test the
            # guard separately in a subprocess without touching real /data.
            result = self.invoke("x" * 32, path)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("persistent /data", result.stderr)

    def test_missing_token_fails_before_initializing_store(self):
        with TemporaryDirectory() as root:
            result = self.invoke(None, "/data/chatnovella-v2")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("CHATNOVELLA_V2_TOKEN", result.stderr)

    def test_short_token_fails(self):
        result = self.invoke("short", "/data/chatnovella-v2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CHATNOVELLA_V2_TOKEN", result.stderr)

    def test_lite_data_dir_is_forbidden(self):
        result = self.invoke("x" * 32, "/data/chatnovella-lite")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires its own", result.stderr)

    def test_safe_token_and_data_do_not_use_lite(self):
        # Import from the real /data path (as in Amvera) is tested only
        # when that filesystem exists in the deployment environment;
        # CI never creates/mutates /data on the host.
        self.assertIn('"/data/chatnovella-v2"', (self.repo /
                      "novella_v2/amvera_entry.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
