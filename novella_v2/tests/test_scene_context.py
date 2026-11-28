"""Two-role packet tests against temporary V2 storage; no real novel data."""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novella_v2.engine import Engine, EngineError, CHUNK_SIZE, digest, empty_actor


def state(turn):
    return {"turn": turn, "datetime": None, "location": "Комната",
            "pov": empty_actor(), "present": {"adrian": empty_actor()},
            "remote": {}, "objects": ["зеркало"], "unfinished": []}


def compilation(*ids, quotes=None):
    quotes = quotes or {"adrian": "Адриан ревнив и вспыльчив.",
                        "silas": "Сайлас видит отражения."}
    def dossier(name):
        return (f"### {name}\n"
                f"Канон: ФАКТ: сведения о {name}.\n"
                f"Цитата: {quotes.get(name, 'нет отдельной карточки' if name == 'pov' else 'предложение')}\n"
                "Внешность: подробности не заданы.\n"
                "Знания: только доступные лично.\n"
                "Отношения: не установлены.\n"
                "Состояние: находится в комнате или кандидат.\n"
                "Мотивы: ИНТЕРПРЕТАЦИЯ: текущая цель.\n"
                "Поведение: ВОЗМОЖНОСТЬ: ответ на приветствие.\n"
                "Границы: не передавать чужие знания.\n")
    return ("## Текущий момент\nВвод: Привет.\n"
            "## Предыстория\nПервый разговор.\n"
            "## Пространство\nКомната, зеркало.\n"
            "## Персонажи\n" + dossier("pov") +
            "".join(dossier(name) for name in ids) +
            "## Мир и сюжет\nОткрытые процессы учитываются.\n"
            "## Возможности режиссёра\nКандидат не означает появление.\n"
            "## Ограничения\nНе передавать чужие знания.\n")


class TestSceneContext(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.instructions = root / "instructions"
        self.instructions.mkdir()
        for name, text in {"rules.md": "Технические правила",
                           "scene_builder.md": "Инструкция сборщика",
                           "skin_builder.md": "Художественный текст 🌟"}.items():
            (self.instructions / name).write_text(text, encoding="utf-8")
        self.engine = Engine(root / "sessions", self.instructions,
                             scene_review_enabled=False, scene_context_enabled=True)
        source = "Мир.\nАдриан ревнив и вспыльчив.\nСайлас видит отражения.\n"
        created = self.engine.create(source, [
            {"anchor": "Мир.", "target": "novel"},
            {"anchor": "Адриан", "target": "adrian"},
            {"anchor": "Сайлас", "target": "silas"}],
            {"adrian": "Адриан", "silas": "Сайлас"}, relationships={
                "familiarity": {"adrian": {"pov": "acquainted"}, "silas": {"pov": "acquainted"}},
                "npc_to_pov": {"adrian": {"ревность": 87}, "silas": {"интерес": 24}},
                "npc_to_npc": {}})
        self.sid = created["session_id"]
        self.folder = root / "sessions" / self.sid
        self.engine.approve(self.sid, created["version"], created["canon_hash"], "Подтверждаю")
        self.engine.set_opening(self.sid, self.engine.status(self.sid)["version"], state(0))

    def raw(self, req="test-1", participants=None):
        self.engine.director_brief(self.sid)
        return self.engine.prepare(self.sid, "Привет", participants or [], request_id=req)

    def read(self, packet):
        parts = []
        for i in packet["unread_chunk_indices"]:
            chunk = self.engine.read_chunk(self.sid, packet["packet_id"], i)
            self.assertEqual(chunk["sha256"], packet["chunk_hashes"][i])
            self.assertLessEqual(len(chunk["content"]), CHUNK_SIZE)
            parts.append(chunk["content"])
        joined = "".join(parts)
        self.assertEqual(digest(joined), packet["sha256"])
        return json.loads(joined)

    def submit(self, raw, text=None, ids=None, proposals=None):
        ids = ids if ids is not None else ["adrian", "silas"]
        text = text if text is not None else compilation(*ids)
        return self.engine.submit_scene_context(
            self.sid, raw["version"], raw["packet_id"], raw["request_id"],
            raw["chunk_hashes"], text, ids, None, proposals)

    def turn(self, packet, n=1):
        return {"request_id": packet["request_id"], "expected_version": packet["version"],
                "packet_id": packet["packet_id"], "chunk_hashes": packet["chunk_hashes"],
                "reader_text": "Привет", "narration": "**Адриан** — Привет.",
                "participants": ["adrian"], "state": state(n),
                "knowledge": [], "events": [], "relations": []}

    def assert_code(self, code, fn, *args):
        with self.assertRaises(EngineError) as caught:
            fn(*args)
        self.assertEqual(caught.exception.code, code)

    def test_roundtrip_requires_both_packets_and_does_not_commit_candidates(self):
        raw = self.raw()
        self.assertEqual(raw["phase"], "collector")
        self.assertIsNone(raw["skin_chunk_start"])
        self.assert_code("PACKET_INCOMPLETE", self.submit, raw)
        payload = self.read(raw)
        self.assertNotIn("skin_builder", payload)
        self.assertIn("scene_builder", payload)
        self.assertIn("characters/silas.md", payload["files"])
        self.assertIn("knowledge/silas.md", payload["files"])
        self.assertNotIn("silas", raw["participants"])
        self.assert_code("SCENE_CONTEXT_REQUIRED", self.engine.commit, self.sid, self.turn(raw))
        before = {p: p.read_bytes() for p in self.folder.rglob("*.md")
                  if "system" not in p.relative_to(self.folder).parts}
        director = self.submit(raw)
        self.assertEqual(director["phase"], "director")
        self.assertNotEqual(raw["packet_id"], director["packet_id"])
        self.assertEqual(director["version"], raw["version"])
        self.assertEqual(self.engine.status(self.sid)["turn"], 0)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(self.submit(raw)["packet_id"], director["packet_id"])
        self.assertEqual(self.raw()["packet_id"], director["packet_id"])
        self.assert_code("PACKET_EXPIRED", self.engine.commit, self.sid, self.turn(raw))
        self.assert_code("PACKET_INCOMPLETE", self.engine.commit, self.sid, self.turn(director))
        writing = self.read(director)
        self.assertEqual(writing["scene_context"]["content"], compilation("adrian", "silas"))
        self.assertEqual(writing["reader_text"], "Привет")
        self.assertEqual(list(writing)[-1], "skin_builder")
        self.assertNotIn("files", writing)
        self.assertEqual(writing["continuity"]["state"], state(0))
        self.assertEqual(writing["continuity"]["relationships"]["npc_to_pov"]["adrian"], {"ревность": 87})
        self.assertEqual(writing["skin_builder"]["content"], "Художественный текст 🌟")
        self.assertEqual((self.folder / "system/scene_context.md").read_bytes().decode("utf-8"),
                         compilation("adrian", "silas"))
        saved = self.engine.commit(self.sid, self.turn(director))
        self.assertTrue(saved["saved"])
        self.assertEqual(saved, self.engine.commit(self.sid, self.turn(director)))
        self.assertEqual(self.engine.history(self.sid)[0]["participants"], ["adrian"])
        self.assertNotIn("scene_context", self.engine.history(self.sid)[0])
        fresh = self.raw("test-2")
        self.assertEqual(fresh["phase"], "collector")
        next_data = self.read(fresh)
        self.assertNotIn("scene_context", next_data)
        self.assertEqual(next_data["recent_scenes"][-1]["narration"], "**Адриан** — Привет.")

    def test_reject_empty_missing_dossier_unknown_actor_and_oversize(self):
        raw = self.raw()
        self.read(raw)
        self.assert_code("SCENE_CONTEXT_SIZE", self.submit, raw, "")
        self.assert_code("SCENE_CONTEXT_SECTIONS", self.submit, raw, "Не сборка")
        self.assert_code("SCENE_CONTEXT_ACTORS", self.submit, raw, compilation("adrian"))
        self.assert_code("SCENE_CONTEXT_ACTORS", self.submit, raw, compilation("silas"), ["silas"])
        self.assert_code("UNKNOWN_CHARACTER", self.submit, raw, compilation("adrian", "unknown"),
                         ["adrian", "unknown"])
        self.assert_code("SCENE_CONTEXT_DOSSIER", self.submit, raw,
                         compilation("adrian", "silas").replace("Мотивы:", "Пропуск:", 1))
        self.assert_code("SCENE_CONTEXT_EVIDENCE", self.submit, raw,
                         compilation("adrian", "silas").replace("Адриан ревнив и вспыльчив.",
                                                               "Адриан всегда спокоен.", 1))
        valid = compilation("adrian", "silas")
        limit = valid + "🌟" * (CHUNK_SIZE - len(valid))
        self.assert_code("SCENE_CONTEXT_SIZE", self.submit, raw, limit + "🌟")
        director = self.submit(raw, limit)
        self.assertEqual(self.read(director)["scene_context"]["chars"], CHUNK_SIZE)

    def test_optional_new_character_stays_temporary(self):
        raw = self.raw()
        self.read(raw)
        proposal = {"name": "marcus", "display": "Маркус",
                    "established_text": "Маркус остановился у дверей."}
        director = self.submit(raw, ids=["adrian", "marcus"], proposals=[proposal])
        self.assertFalse((self.folder / "characters/marcus.md").exists())
        writing = self.read(director)
        self.assertEqual(writing["new_character_proposals"], [proposal])
        self.engine.commit(self.sid, self.turn(director))
        self.assertFalse((self.folder / "characters/marcus.md").exists())

    def test_changed_instructions_and_tampered_compilation_fail_closed(self):
        raw = self.raw()
        self.read(raw)
        (self.instructions / "scene_builder.md").write_text("Новая сборка", encoding="utf-8")
        self.assert_code("SCENE_BUILDER_VERSION_CHANGED", self.submit, raw)
        fresh = self.raw()
        self.assertNotEqual(fresh["packet_id"], raw["packet_id"])
        self.read(fresh)
        director = self.submit(fresh)
        self.read(director)
        (self.folder / "system/scene_context.md").write_text("Подмена", encoding="utf-8")
        self.assert_code("SCENE_CONTEXT_CHANGED", self.engine.commit, self.sid, self.turn(director))
        self.assertEqual(self.raw()["phase"], "collector")
        self.assertEqual(self.engine.status(self.sid)["turn"], 0)

    def test_changed_unselected_card_invalidates_collection(self):
        raw = self.raw()
        self.read(raw)
        card = self.folder / "characters/silas.md"
        card.write_text("Подмена", encoding="utf-8")
        self.assert_code("SOURCE_CHANGED", self.submit, raw, compilation("adrian"), ["adrian"])

    def test_canon_unchanged_and_mode_switch_rejects_legacy_packet(self):
        canon = self.engine.prepare(self.sid, kind="canon")
        data = self.read(canon)
        self.assertNotIn("scene_builder", data)
        self.assertNotIn("skin_builder", data)
        self.engine.scene_context_enabled = False
        legacy = self.raw()
        self.read(legacy)
        self.engine.scene_context_enabled = True
        self.assert_code("CONTEXT_MODE_CHANGED", self.engine.commit, self.sid, self.turn(legacy))

    def test_exact_continuity_does_not_include_unselected_relationships(self):
        raw = self.raw()
        self.read(raw)
        director = self.submit(raw, ids=["adrian"])
        writing = self.read(director)
        self.assertEqual(writing["continuity"]["state"], state(0))
        self.assertEqual(writing["continuity"]["relationships"]["npc_to_pov"],
                         {"adrian": {"ревность": 87}})
        self.assertNotIn("silas", writing["continuity"]["relationships"]["familiarity"])

    def test_initial_proposal_can_be_dropped_or_repeated_without_duplicate(self):
        proposal = {"name": "marcus", "display": "Маркус",
                    "established_text": "Маркус остановился у дверей."}
        self.engine.director_brief(self.sid)
        raw = self.engine.prepare(self.sid, "Привет", ["marcus"],
                                  request_id="test-1", new_characters=[proposal])
        self.read(raw)
        director = self.submit(raw, ids=["adrian"])
        self.assertEqual(self.read(director)["new_character_proposals"], [])
        # New uncommitted request supersedes the scratch packet, not game history.
        raw2 = self.engine.prepare(self.sid, "Привет", ["marcus"],
                                   request_id="test-2", new_characters=[proposal])
        self.read(raw2)
        director2 = self.submit(raw2, ids=["adrian", "marcus"], proposals=[proposal])
        self.assertEqual(self.read(director2)["new_character_proposals"], [proposal])
        self.assertFalse((self.folder / "characters/marcus.md").exists())

    def test_author_reminder_appearing_mid_turn_invalidates_both_phases(self):
        for n in (1, 2):
            raw = self.raw(f"turn-{n}")
            self.read(raw)
            director = self.submit(raw, ids=["adrian"])
            self.read(director)
            self.engine.commit(self.sid, self.turn(director, n))
        raw = self.raw("turn-3")
        self.read(raw)
        reminder = self.instructions / "author_reminder.md"
        reminder.write_text("Новый сигнал", encoding="utf-8")
        self.assert_code("RULES_VERSION_CHANGED", self.submit, raw)
        fresh = self.raw("turn-3")
        self.read(fresh)
        director = self.submit(fresh, ids=["adrian"])
        self.assertEqual(self.read(director)["author_reminder"], "Новый сигнал")
        reminder.unlink()
        self.assert_code("RULES_VERSION_CHANGED", self.engine.commit, self.sid,
                         self.turn(director, 3))

    def test_changed_submission_cannot_reuse_receipt(self):
        raw = self.raw()
        self.read(raw)
        self.submit(raw)
        self.assert_code("SCENE_CONTEXT_REUSED", self.submit, raw, compilation("adrian", "silas") + "Другое")
