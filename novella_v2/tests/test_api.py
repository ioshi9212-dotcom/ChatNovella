"""Full HTTP workflow tests against temporary V2 data; no production server calls."""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import novella_v2.api as api
from novella_v2.engine import Engine, empty_actor


def state(turn):
    return {
        "turn": turn, "datetime": "2026-09-12T18:00:00", "location": "Гостиная",
        "pov": empty_actor(),
        "present": {"adrian": empty_actor()}, "remote": {},
        "objects": ["ключ на столе"], "unfinished": [],
    }


class TestV2Http(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        rules = root / "rules"
        rules.mkdir()
        (rules / "rules.md").write_text("Механика", encoding="utf-8")
        (rules / "skin_builder.md").write_text("Только Skin Builder", encoding="utf-8")
        self.new_store = Engine(root / "data", rules)
        self.addCleanup(patch.stopall)
        patch.object(api, "store", self.new_store).start()
        patch.object(api, "TOKEN", "t" * 32).start()
        self.client = TestClient(api.app)
        self.auth = {"Authorization": "Bearer " + "t" * 32}

    def make_novel(self, original=None):
        original = original or "Мир и город.\r\nАдриан жёсткий, упрямый.\r\n"
        second = original.index("Адриан")
        r = self.client.post("/v2/novels", headers=self.auth, json={
            "original": original,
            "blocks": [
                {"start": 0, "end": second, "target": "novel"},
                {"start": second, "end": len(original), "target": "adrian"},
            ],
            "names": {"adrian": "Адриан"},
            "genre_answers": {"хоррор": "высокая интенсивность"},
        })
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def start_novel(self):
        created = self.make_novel()
        sid = created["session_id"]
        confirm = self.client.post("/v2/novels/" + sid + "/approve",
                                   headers=self.auth,
                                   json={"expected_version": created["version"],
                                         "canon_hash": created["canon_hash"],
                                         "confirmation": "Утверждаю"})
        self.assertEqual(confirm.status_code, 200, confirm.text)
        opening = self.client.post("/v2/novels/" + sid + "/opening",
                                   headers=self.auth,
                                   json={"expected_version": confirm.json()["version"],
                                         "state": state(0)})
        self.assertEqual(opening.status_code, 200, opening.text)
        return sid

    def prepared(self, sid, reader="Привет", participants=None, kind="turn"):
        r = self.client.post(f"/v2/novels/{sid}/prepare", headers=self.auth,
                             json={"reader_text": reader, "participants": participants or ["adrian"],
                                   "kind": kind})
        self.assertEqual(r.status_code, 200, r.text)
        packet = r.json()
        content = []
        for index in range(packet["chunk_count"]):
            res = self.client.get(f"/v2/novels/{sid}/chunks/{packet['packet_id']}/{index}",
                                  headers=self.auth)
            self.assertEqual(res.status_code, 200, res.text)
            self.assertLessEqual(len(res.json()["content"]), 36000)
            content.append(res.json()["content"])
        return packet, "".join(content)

    def test_healthz_is_public(self):
        self.assertEqual(self.client.get("/healthz").json()["engine"], "chatnovella-v2")

    def test_all_private_routes_require_token(self):
        self.assertEqual(self.client.get("/openapi.json").status_code, 401)
        self.assertEqual(self.client.get("/v2/novels/NV2_" + "a" * 24).status_code, 401)
        self.assertEqual(self.client.post("/v2/novels", json={}).status_code, 401)
        self.assertEqual(self.client.get("/openapi.json", headers=self.auth).status_code, 200)

    def test_wrong_bearer_token_is_rejected(self):
        self.assertEqual(self.client.get("/openapi.json", headers={
            "Authorization": "Bearer " + "x" * 32}).status_code, 401)

    def test_server_fails_closed_when_token_missing(self):
        with patch.object(api, "TOKEN", ""):
            self.assertEqual(self.client.get("/openapi.json").status_code, 503)
            self.assertEqual(self.client.post("/v2/novels", json={}).status_code, 503)
            self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_http_preapproval_preview_is_exact(self):
        src = "Петербург. 🐺\r\nАдриан хмурится.\r\n"
        created = self.make_novel(src)
        sid = created["session_id"]
        packet, content = self.prepared(sid, reader="", participants=[], kind="canon")
        import json
        files = json.loads(content)["files"]
        self.assertEqual(files["original.txt"], src)
        self.assertIn("Адриан хмурится.", files["characters/adrian.md"])
        self.assertEqual(packet["chunk_size"], 36000)

    def test_http_full_flow_with_memory_timeline_relations_and_rollback(self):
        sid = self.start_novel()
        p, content = self.prepared(sid)
        self.assertIn("Только Skin Builder", content)
        scene = "**Адриан** — Встречаемся завтра."
        body = {
            "request_id": "remote-req-1",
            "expected_version": p["version"],
            "packet_id": p["packet_id"],
            "chunk_hashes": p["chunk_hashes"],
            "reader_text": "Привет",
            "narration": scene,
            "participants": ["adrian"],
            "state": state(1),
            "knowledge": [{
                "owner": "adrian",
                "fact": "Договорился о встрече",
                "source": "Встречаемся завтра.",
            }],
            "events": [{
                "text": "Адриан назначил встречу на завтра.",
                "participants": ["adrian", "pov"],
            }],
            "relations": [{
                "source": "adrian", "target": "pov", "metric": "доверие",
                "value": 60, "reason": "Открытый разговор",
            }],
        }
        r = self.client.post(f"/v2/novels/{sid}/turns", headers=self.auth, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["turn"], 1)
        repeat = self.client.post(f"/v2/novels/{sid}/turns", headers=self.auth, json=body)
        self.assertEqual(repeat.json(), r.json())
        history = self.client.get(f"/v2/novels/{sid}/history",
                                  headers=self.auth, params={"query": "Встречаемся"})
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(len(history.json()["scenes"]), 1)
        newpacket, newcontent = self.prepared(sid, reader="Что потом?")
        self.assertIn("Договорился о встрече", newcontent)
        self.assertIn("Адриан назначил встречу", newcontent)
        self.assertIn("доверие", newcontent)
        self.assertGreater(newpacket["version"], p["version"])
        undo = self.client.post(f"/v2/novels/{sid}/rollback", headers=self.auth,
                                json={"expected_version": r.json()["version"],
                                      "request_id": body["request_id"],
                                      "reason": "Отменить этот ход"})
        self.assertEqual(undo.status_code, 200, undo.text)
        status = self.client.get(f"/v2/novels/{sid}", headers=self.auth).json()
        self.assertEqual(status["turn"], 0)
        scenes = self.client.get(f"/v2/novels/{sid}/history", headers=self.auth).json()
        self.assertEqual(scenes["scenes"], [])

    def test_http_same_request_resumes_and_replays_without_regeneration(self):
        sid = self.start_novel()
        url = f"/v2/novels/{sid}/prepare"
        body = {"reader_text": "Привет", "participants": ["adrian"],
                "kind": "turn", "request_id": "http-resume-1"}
        first = self.client.post(url, headers=self.auth, json=body)
        self.assertEqual(first.status_code, 200, first.text)
        prepared = first.json()
        self.assertFalse(prepared["all_chunks_read"])
        second = self.client.post(url, headers=self.auth, json=body)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["packet_id"], prepared["packet_id"])
        self.assertTrue(second.json()["reused_pending_packet"])
        for index in second.json()["unread_chunk_indices"]:
            result = self.client.get(
                f"/v2/novels/{sid}/chunks/{prepared['packet_id']}/{index}",
                headers=self.auth)
            self.assertEqual(result.status_code, 200)
        ready = self.client.post(url, headers=self.auth, json=body).json()
        self.assertTrue(ready["all_chunks_read"])
        scene = "**Адриан** — Получено."
        saved = self.client.post(f"/v2/novels/{sid}/turns", headers=self.auth, json={
            "request_id": body["request_id"],
            "expected_version": prepared["version"],
            "packet_id": prepared["packet_id"],
            "chunk_hashes": prepared["chunk_hashes"],
            "reader_text": body["reader_text"],
            "narration": scene,
            "participants": ["adrian"],
            "state": state(1),
            "knowledge": [], "events": [], "relations": [],
        })
        self.assertEqual(saved.status_code, 200, saved.text)
        replay = self.client.post(url, headers=self.auth, json=body)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["already_committed_duplicate"])
        self.assertEqual(replay.json()["narration"], scene)
        self.assertNotIn("packet_id", replay.json())
        self.assertEqual(self.client.get(
            f"/v2/novels/{sid}/history", headers=self.auth).json()["scenes"][0]["narration"], scene)

    def test_http_invalid_session_returns_client_error(self):
        self.assertEqual(self.client.get("/v2/novels/INVALID", headers=self.auth).status_code, 422)

    def test_http_missing_required_body_does_not_crash_server(self):
        r = self.client.post("/v2/novels", headers=self.auth, json={})
        self.assertEqual(r.status_code, 422, r.text)

    def test_http_36k_unicode_output(self):
        source = "Мир.\nАдриан " + ("Б" * 90_000)
        created = self.make_novel(source)
        p, value = self.prepared(created["session_id"], reader="", participants=[], kind="canon")
        self.assertGreaterEqual(p["chunk_count"], 3)
        import json
        self.assertEqual(json.loads(value)["files"]["original.txt"], source)


if __name__ == "__main__":
    unittest.main()
