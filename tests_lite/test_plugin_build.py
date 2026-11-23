"""Check the shipped protocol, not just whether the ZIP can be opened."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]


class PluginBuildTests(unittest.TestCase):
    def test_archive_uses_live_lite_instructions_and_preserves_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'chatnovella.zip'
            subprocess.run([sys.executable, str(ROOT / 'scripts/build_plugin.py'), str(output)],
                           cwd=folder, check=True, capture_output=True, text=True)
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                workflow = archive.read('chatnovella/skills/chatnovella/references/workflow.md')
                self.assertEqual(workflow, (ROOT / 'docs/lite/INSTRUCTIONS.md').read_bytes())
                for field in ('knowledge_review', 'significance_reason',
                              'knowledge/POV_001.md', 'audit_compaction'):
                    self.assertIn(field, workflow.decode('utf-8'))
                for name in ('plugin.json', 'mcp.json'):
                    shipped = json.loads(archive.read('chatnovella/' + name))
                    original = json.loads((ROOT / 'plugin/chatnovella' / name).read_bytes())
                    self.assertEqual(shipped, original)


if __name__ == '__main__':
    unittest.main()
