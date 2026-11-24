"""Smoke tests for dedicated V2 Amvera entrypoint, no API credentials."""
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

    def invoke(self, directory: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["CHATNOVELLA_V2_DATA_DIR"] = directory
        script = (
            "from fastapi.testclient import TestClient; "
            "from novella_v2.amvera_entry import app; "
            "client=TestClient(app); "
            "assert client.get('/healthz').json()['engine']=='chatnovella-v2'; "
            "response=client.get('/v2/instructions'); "
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

    def test_path_outside_persistent_mount_fails(self):
        with TemporaryDirectory() as root:
            result = self.invoke(os.path.join(root, "v2"))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("persistent /data", result.stderr)

    def test_shared_data_root_forbidden(self):
        result = self.invoke("/data")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("shared /data root", result.stderr)

    def test_lite_data_dir_is_forbidden(self):
        result = self.invoke("/data/chatnovella-lite")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires its own", result.stderr)

    def test_no_authentication_setup_in_entrypoint(self):
        entry = (self.repo / "novella_v2/amvera_entry.py").read_text(encoding="utf-8")
        self.assertIn('"/data/chatnovella-v2"', entry)
        self.assertNotIn("CHATNOVELLA_V2_TOKEN", entry)
        self.assertNotIn("Bearer", entry)


if __name__ == "__main__":
    unittest.main()
