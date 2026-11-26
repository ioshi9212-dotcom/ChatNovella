"""Chat-scoped instruction retrieval: no full Rules/Skin Builder in every turn."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import novella_v2.api as api
from novella_v2.engine import Engine, empty_actor


def scene_state(n=0):
    return {
        "turn": n, "datetime": "2026-11-23T20:00:00",
        "location": "Комната", "pov": empty_actor(),
        "present": {"adrian": empty_actor()}, "remote": {},
        "objects": [], "unfinished": [],
    }


class TestChatInstructions(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        self.files = folder / "runtime"
        self.files.mkdir()
        self.rules = self.files / "rules.md"
        self.skin = self.files / "skin_builder.md"
        self.rules.write_text("Механика: исходные правила.\n", encoding="utf-8")
        # The author explicitly wants this V2 file blank until they fill it.
        self.skin.write_bytes(b"")
        self.engine = Engine(folder / "sessions", self.files)
        source = "Мир.\nАдриан злой.\n"
        offset = source.index("Адриан")
        result = self.engine.create(
            source, [{"start": 0, "end": offset, "target": "novel"},
                     {"start": offset, "end": len(source), "target": "adrian"}],
            {"adrian": "Адриан"})
        self.sid = result["session_id"]
        self.engine.approve(self.sid, result["version"],
                            result["canon_hash"], "Утверждаю")
        self.engine.set_opening(self.sid, self.engine.status(self.sid)["version"],
                                scene_state(0))

    def packet(self, request_id="chat-1"):
        self.engine.director_brief(self.sid)
        return self.engine.prepare(
            self.sid, "Привет", ["adrian"], request_id=request_id)

    def read_packet(self, packet):
        return json.loads("".join(
            self.engine.read_chunk(self.sid, packet["packet_id"], i)["content"]
            for i in range(packet["chunk_count"])))

    def test_initial_chat_read_returns_both_complete_texts(self):
        data = self.engine.load_instructions()
        self.assertEqual(data["refresh_policy"], "manual_per_chat")
        self.assertEqual(data["rules"]["content"], "Механика: исходные правила.\n")
        self.assertEqual(data["skin_builder"]["content"], "")
        self.assertEqual(data["skin_builder"]["chars"], 0)
        self.assertTrue(data["skin_builder"]["empty"])
        self.assertEqual(data["skin_builder"]["sha256"],
                         sha256(b"").hexdigest())

    def test_initial_empty_builder_does_not_fallback_to_old_builder(self):
        # The old copy is deliberately placed nearby. No code may auto-load it.
        legacy = self.files.parent / "engine"
        legacy.mkdir()
        (legacy / "skin_builder.md").write_text(
            "СТАРЫЕ ХУДОЖЕСТВЕННЫЕ ПРАВИЛА", encoding="utf-8")
        result = self.engine.load_instructions()
        self.assertEqual(result["skin_builder"]["content"], "")
        packet = self.packet()
        context = self.read_packet(packet)
        self.assertNotIn("СТАРЫЕ ХУДОЖЕСТВЕННЫЕ ПРАВИЛА", json.dumps(context, ensure_ascii=False))

    def test_versions_returns_only_hashes_never_full_instructions(self):
        meta = self.engine.instruction_versions()
        self.assertEqual(set(meta),
                         {"rules_sha256", "skin_builder_sha256", "skin_builder_empty"})
        self.assertTrue(meta["skin_builder_empty"])
        self.assertEqual(meta["rules_sha256"],
                         sha256(self.rules.read_bytes()).hexdigest())

    def test_explicit_refresh_returns_changed_rule_and_builder(self):
        initial = self.engine.load_instructions()
        self.rules.write_text("Новые правила. 🌟", encoding="utf-8")
        self.skin.write_text("Новый Skin Builder: текст пользователя", encoding="utf-8")
        metadata = self.engine.instruction_versions()
        self.assertNotEqual(initial["rules"]["sha256"], metadata["rules_sha256"])
        self.assertNotEqual(initial["skin_builder"]["sha256"],
                            metadata["skin_builder_sha256"])
        updated = self.engine.load_instructions()
        self.assertEqual(updated["rules"]["content"], "Новые правила. 🌟")
        self.assertEqual(updated["skin_builder"]["content"],
                         "Новый Skin Builder: текст пользователя")
        self.assertFalse(updated["skin_builder"]["empty"])

    def test_rules_builder_never_appear_in_game_packet(self):
        packet = self.packet()
        context = self.read_packet(packet)
        self.assertEqual(context["instructions_mode"], "loaded_once_per_chat")
        self.assertNotIn("engine/rules.md", context["files"])
        self.assertNotIn("engine/skin_builder.md", context["files"])
        self.assertIn("novel.md", context["files"])
        self.assertIn("characters/adrian.md", context["files"])
        self.assertNotIn(self.rules.read_text(encoding="utf-8"),
                         json.dumps(context, ensure_ascii=False))

    def test_prepare_does_not_read_any_instruction_files(self):
        instruction_reads = []
        previous = Engine._read

        def observed(path, name):
            if name in ("rules.md", "skin_builder.md"):
                instruction_reads.append(name)
            return previous(path, name)

        with patch.object(Engine, "_read", side_effect=observed):
            packet = self.packet()
            self.read_packet(packet)
        self.assertEqual(instruction_reads, [])

    def test_editing_instructions_does_not_invalidate_prepared_packet(self):
        first = self.packet()
        self.rules.write_text("Правила 2027", encoding="utf-8")
        self.skin.write_text("Новый Skin Builder", encoding="utf-8")
        reused = self.packet()
        self.assertEqual(first["packet_id"], reused["packet_id"])
        self.assertTrue(reused["reused_pending_packet"])
        self.assertEqual(first["sha256"], reused["sha256"])

    def test_instruction_change_does_not_block_commit(self):
        packet = self.packet()
        self.read_packet(packet)
        self.skin.write_text("Обновлено по команде читателя", encoding="utf-8")
        # Even a minimal nonempty authored builder requires an explicit review.
        self.engine.review_scene(
            self.sid, packet["version"], packet["packet_id"], "chat-1",
            sha256(self.skin.read_bytes()).hexdigest(),
            "**Адриан** — Здравствуй.",
            [{"section": "SKIN BUILDER: ВЕСЬ ТЕКСТ", "result": "pass",
              "note": "Вся актуальная пользовательская инструкция проверена."}],
            [{"id": "L1", "result": "pass",
              "note": "Проверена авторская инструкция по сцене.",
              "evidence": "**Адриан** — Здравствуй."}])
        result = self.engine.commit(self.sid, {
            "request_id": "chat-1", "expected_version": packet["version"],
            "packet_id": packet["packet_id"], "chunk_hashes": packet["chunk_hashes"],
            "reader_text": "Привет", "narration": "**Адриан** — Здравствуй.",
            "participants": ["adrian"], "state": scene_state(1),
            "knowledge": [], "events": [], "relations": [],
        })
        self.assertTrue(result["saved"])

    def test_new_game_packet_after_instruction_update_still_excludes_full_text(self):
        self.skin.write_text("ХУДОЖЕСТВЕННЫЙ ТЕКСТ " * 5000, encoding="utf-8")
        packet = self.packet()
        content = self.read_packet(packet)
        self.assertNotIn("skin_builder.md", content["files"])
        self.assertLess(len(json.dumps(content, ensure_ascii=False)), 36_000)
        self.assertEqual(self.engine.load_instructions()["skin_builder"]["chars"],
                         len("ХУДОЖЕСТВЕННЫЙ ТЕКСТ " * 5000))


class TestChatInstructionsHttp(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.files = Path(temporary.name) / "runtime"
        self.files.mkdir()
        self.rules = self.files / "rules.md"
        self.skin = self.files / "skin_builder.md"
        self.rules.write_text("Rules v1", encoding="utf-8")
        self.skin.write_bytes(b"")
        engine = Engine(Path(temporary.name) / "sessions", self.files)
        self.addCleanup(patch.stopall)
        patch.object(api, "store", engine).start()
        self.http = TestClient(api.app)
        self.headers = {}

    def test_instruction_routes_are_available_without_token(self):
        for route in ("/v2/instructions", "/v2/instructions/versions"):
            self.assertEqual(self.http.get(route).status_code, 200)

    def test_http_load_and_refresh_without_automatic_turn_fetches(self):
        first = self.http.get("/v2/instructions", headers=self.headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["rules"]["content"], "Rules v1")
        self.assertEqual(first.json()["skin_builder"]["content"], "")
        hashes = self.http.get("/v2/instructions/versions", headers=self.headers)
        self.assertEqual(hashes.status_code, 200)
        self.assertEqual(hashes.json()["rules_sha256"],
                         first.json()["rules"]["sha256"])
        self.rules.write_text("Rules v2", encoding="utf-8")
        self.skin.write_text("Skin Builder v2", encoding="utf-8")
        updated = self.http.get("/v2/instructions", headers=self.headers)
        self.assertEqual(updated.json()["rules"]["content"], "Rules v2")
        self.assertEqual(updated.json()["skin_builder"]["content"], "Skin Builder v2")
        self.assertNotEqual(updated.json()["rules"]["sha256"],
                            first.json()["rules"]["sha256"])
        self.assertEqual(self.http.get("/v2/instructions/versions",
                                       headers=self.headers).json()["skin_builder_sha256"],
                         updated.json()["skin_builder"]["sha256"])

    def test_http_instruction_routes_work_without_authorization_header(self):
        self.assertEqual(self.http.get("/v2/instructions").status_code, 200)
        self.assertEqual(self.http.get("/v2/instructions/versions").status_code, 200)


if __name__ == "__main__":
    unittest.main()
