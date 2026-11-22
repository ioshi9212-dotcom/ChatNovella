"""Release checks: both plugin formats must ship the exact current engine skin."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

from scripts.build_plugin import build, SKIN_PATH


class PluginPackageTests(unittest.TestCase):
    def test_both_packages_preserve_connections_and_full_skin(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            for directory in ("plugin", "docs", "engine"):
                shutil.copytree(repo / directory, root / directory)
            for variant in ("chatnovella", "chatnovella-amvera"):
                with self.subTest(variant=variant):
                    source = root / "plugin" / variant
                    original = json.loads((source / "plugin.json").read_text())
                    archive_path = build(root, Path(temp) / (variant + ".zip"), variant)
                    prefix = original["name"] + "/"
                    with zipfile.ZipFile(archive_path) as archive:
                        self.assertIsNone(archive.testzip())
                        self.assertEqual(json.loads(archive.read(prefix + "plugin.json")), original)
                        self.assertEqual(archive.read(prefix + SKIN_PATH), (root / "engine/skin_builder.md").read_bytes())
                        connection = ".app.json" if variant.endswith("-amvera") else "mcp.json"
                        self.assertEqual(archive.read(prefix + connection), (source / connection).read_bytes())
                        self.assertEqual(len([n for n in archive.namelist() if n.endswith("SKILL.md")]), 1)

    def test_rebuild_updates_skin_and_hash_without_duplicate_instructions(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            for directory in ("plugin", "docs", "engine"):
                shutil.copytree(repo / directory, root / directory)
            skin = (root / "engine/skin_builder.md").read_bytes() + "\nНовая редакция.\n".encode()
            (root / "engine/skin_builder.md").write_bytes(skin)
            output = Path(temp) / "amvera.zip"
            build(root, output, "chatnovella-amvera")
            build(root, output, "chatnovella-amvera")
            source = root / "plugin/chatnovella-amvera"
            skill = (source / "skills/chatnovella/SKILL.md").read_text()
            self.assertEqual(skill.count("<!-- BUNDLED_SKIN_START -->"), 1)
            self.assertIn(hashlib.sha256(skin).hexdigest(), skill)
            self.assertEqual((source / SKIN_PATH).read_bytes(), skin)


if __name__ == "__main__":
    unittest.main()
