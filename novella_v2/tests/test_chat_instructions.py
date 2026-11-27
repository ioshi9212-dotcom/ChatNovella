"""Ordered per-turn instruction snapshots and chronological scene context."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import novella_v2.api as api
from novella_v2.engine import Engine, EngineError, digest, empty_actor


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
        self.assertEqual(data["refresh_policy"], "rules_first_skin_last_per_turn")
        self.assertFalse(data["scene_review_enabled"])
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
                         {"rules_sha256", "skin_builder_sha256", "skin_builder_empty", "scene_review_enabled"})
        self.assertFalse(meta["scene_review_enabled"])
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

    def save(self, packet, number, narration):
        return self.engine.commit(self.sid, {
            "request_id": packet["request_id"], "expected_version": packet["version"],
            "packet_id": packet["packet_id"], "chunk_hashes": packet["chunk_hashes"],
            "reader_text": "Привет", "narration": narration,
            "participants": ["adrian"], "state": scene_state(number),
            "knowledge": [], "events": [], "relations": [],
        })

    def test_rules_only_bootstrap_never_reads_skin(self):
        original = Engine._read
        def read(path, name):
            self.assertNotEqual(name, "skin_builder.md")
            return original(path, name)
        with patch.object(Engine, "_read", side_effect=read):
            data = self.engine.load_instructions(rules_only=True)
        self.assertEqual(data["rules"]["content"], self.rules.read_text())
        self.assertNotIn("skin_builder", data)

    def test_ordered_turn_packet_rules_recent_scenes_then_skin(self):
        self.skin.write_text("Художественный текст. 🌟", encoding="utf-8")
        packet = self.packet()
        parts = [self.engine.read_chunk(self.sid, packet["packet_id"], i)
                 for i in range(packet["chunk_count"])]
        context = json.loads("".join(p["content"] for p in parts))
        self.assertEqual(list(context)[:2], ["rules", "recent_scenes"])
        self.assertEqual(list(context)[-1], "skin_builder")
        self.assertEqual(context["instructions_mode"], "rules_first_skin_last_per_turn")
        self.assertEqual(context["rules"]["content"], self.rules.read_text())
        self.assertEqual(context["skin_builder"]["content"], self.skin.read_text())
        self.assertEqual(context["skin_builder"]["sha256"], digest(self.skin.read_text()))
        self.assertIn("novel.md", context["files"])
        self.assertIn("characters/adrian.md", context["files"])
        self.assertEqual(parts[-1]["phase"], "skin_builder")
        self.assertTrue(parts[packet["skin_chunk_start"]]["content"].startswith(',"skin_builder":'))
        self.assertTrue(parts[-1]["all_chunks_read"])
        self.assertEqual(digest("".join(p["content"] for p in parts)), packet["sha256"])

    def test_author_reminder_is_loaded_before_skin_every_third_turn(self):
        reminder = self.files / "author_reminder.md"
        reminder.write_text("Читатель знает персонажей.", encoding="utf-8")
        for number in range(1, 7):
            request_id = f"reminder-{number}"
            packet = self.packet(request_id)
            context = self.read_packet(packet)
            self.assertEqual(list(context)[-1], "skin_builder")
            if number % 3 == 0:
                self.assertEqual(list(context)[-2:], ["author_reminder", "skin_builder"])
                self.assertEqual(context["author_reminder"], reminder.read_text())
                self.assertEqual(self.packet(request_id)["packet_id"], packet["packet_id"])
                reminder.write_text(f"Новый сигнал {number}", encoding="utf-8")
                fresh = self.packet(request_id)
                self.assertNotEqual(fresh["packet_id"], packet["packet_id"])
                self.assertEqual(self.read_packet(fresh)["author_reminder"], reminder.read_text())
                packet = fresh
            else:
                self.assertNotIn("author_reminder", context)
            self.assertTrue(self.save(packet, number, f"Сцена {number}.")["saved"])

    def test_author_reminder_cadence_survives_rollback(self):
        (self.files / "author_reminder.md").write_text("Напоминание", encoding="utf-8")
        for number in range(1, 4):
            packet = self.packet(f"scene-{number}")
            self.read_packet(packet)
            self.save(packet, number, f"Сцена {number}.")
        self.engine.rollback(self.sid, self.engine.status(self.sid)["version"],
                             "scene-3", "Отмена третьей сцены")
        context = self.read_packet(self.packet("replacement-3"))
        self.assertEqual(list(context)[-2:], ["author_reminder", "skin_builder"])

    def test_author_reminder_is_not_loaded_for_canon(self):
        (self.files / "author_reminder.md").write_text("Напоминание", encoding="utf-8")
        context = self.read_packet(self.engine.prepare(self.sid, kind="canon"))
        self.assertNotIn("author_reminder", context)

    def test_three_full_active_scenes_in_chronological_order_after_rollback(self):
        for number in range(1, 5):
            packet = self.packet(f"scene-{number}")
            self.read_packet(packet)
            self.save(packet, number, f"Полная сцена {number}.")
        self.engine.rollback(self.sid, self.engine.status(self.sid)["version"],
                             "scene-4", "Отмена четвертой сцены")
        packet = self.packet("scene-5")
        context = self.read_packet(packet)
        self.assertEqual(list(context)[:2], ["rules", "recent_scenes"])
        self.assertEqual([r["turn"] for r in context["recent_scenes"]], [1, 2, 3])
        self.assertEqual([r["narration"] for r in context["recent_scenes"]],
                         [f"Полная сцена {n}." for n in range(1, 4)])
        self.assertEqual([r["reader_text"] for r in context["recent_scenes"]], ["Привет"] * 3)
        self.assertNotIn("Полная сцена 4.", json.dumps(context, ensure_ascii=False))

    def test_cannot_read_final_skin_before_context(self):
        packet = self.packet()
        with self.assertRaisesRegex(EngineError, "next unread chunk"):
            self.engine.read_chunk(self.sid, packet["packet_id"], packet["skin_chunk_start"])
        self.assertEqual(self.packet()["unread_chunk_indices"], list(range(packet["chunk_count"])))
        self.read_packet(packet)

    def test_instruction_change_invalidates_pending_packet_but_not_same_request_retry(self):
        first = self.packet()
        self.assertEqual(first["packet_id"], self.packet()["packet_id"])
        self.rules.write_text("Правила 2027", encoding="utf-8")
        self.skin.write_text("Новый Skin Builder", encoding="utf-8")
        fresh = self.packet()
        self.assertNotEqual(first["packet_id"], fresh["packet_id"])
        self.assertFalse(fresh["reused_pending_packet"])
        context = self.read_packet(fresh)
        self.assertEqual(context["rules"]["content"], "Правила 2027")
        self.assertEqual(context["skin_builder"]["content"], "Новый Skin Builder")

    def test_skin_changed_after_context_before_final_read_is_rejected(self):
        packet = self.packet()
        for i in range(packet["skin_chunk_start"]):
            self.engine.read_chunk(self.sid, packet["packet_id"], i)
        self.skin.write_text("Новая версия", encoding="utf-8")
        with self.assertRaisesRegex(EngineError, "Skin Builder changed"):
            self.engine.read_chunk(self.sid, packet["packet_id"], packet["skin_chunk_start"])

    def test_skin_changed_after_read_cannot_save_old_packet(self):
        packet = self.packet()
        self.read_packet(packet)
        self.skin.write_text("Обновлено по команде читателя", encoding="utf-8")
        with self.assertRaisesRegex(EngineError, "Refresh the active Skin Builder"):
            self.save(packet, 1, "**Адриан** — Здравствуй.")
        fresh = self.packet()
        self.read_packet(fresh)
        self.assertTrue(self.save(fresh, 1, "**Адриан** — Здравствуй.")["saved"])

    def test_every_new_turn_reads_current_skin_not_previous_snapshot(self):
        first = self.packet()
        self.assertEqual(self.read_packet(first)["skin_builder"]["content"], "")
        self.save(first, 1, "Первый ход.")
        self.skin.write_text("Новый авторский текст", encoding="utf-8")
        second = self.packet("chat-2")
        self.assertEqual(self.read_packet(second)["skin_builder"]["content"], "Новый авторский текст")

    def test_long_unicode_builder_and_rules_cross_chunk_boundaries_without_truncation(self):
        self.rules.write_text("Механика 🌟 " * 5000, encoding="utf-8")
        self.skin.write_text("ХУДОЖЕСТВЕННЫЙ ТЕКСТ 🌙 " * 5000, encoding="utf-8")
        packet = self.packet()
        parts = [self.engine.read_chunk(self.sid, packet["packet_id"], i)
                 for i in range(packet["chunk_count"])]
        self.assertGreater(packet["skin_chunk_start"], 1)
        self.assertGreater(len(parts) - packet["skin_chunk_start"], 1)
        self.assertTrue(all(len(p["content"]) <= 36_000 for p in parts))
        text = "".join(p["content"] for p in parts)
        content = json.loads(text)
        self.assertEqual(digest(text), packet["sha256"])
        self.assertEqual(content["rules"]["content"], self.rules.read_text())
        self.assertEqual(content["skin_builder"]["content"], self.skin.read_text())

    def test_canon_has_rules_without_skin(self):
        packet = self.engine.prepare(self.sid, kind="canon")
        context = self.read_packet(packet)
        self.assertEqual(list(context)[0], "rules")
        self.assertNotIn("skin_builder", context)
        self.assertIsNone(packet["skin_chunk_start"])


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
        bootstrap = self.http.get("/v2/instructions?rules_only=true").json()
        self.assertEqual(bootstrap["rules"]["content"], "Rules v1")
        self.assertNotIn("skin_builder", bootstrap)
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
