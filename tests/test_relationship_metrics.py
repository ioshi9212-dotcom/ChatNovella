"""Numeric relationship memory must exist, persist and be recoverable safely."""
import tempfile
import unittest

from review_helpers import reviewed_commit
from pathlib import Path

from chatnovella.store import (
    CHAR_FILES, STATE_FILES, SessionStore, StateError, _atomic_write,
    _validate_relation, _validate_relation_updates, _relation_stats,
)


class TestRelationshipMetrics(unittest.TestCase):
    declared_core = (
        "Эдриан Морвелл. Стартовые чувства Эдриана к Ринате: любовь, "
        "привязанность, сексуальное влечение, тревога за неё, усталость "
        "от её состояния, раздражение и ответственность. Он скрывает чувства."
    )
    partial_relations = "Авторское описание сохранено.\n- любовь 85/+0\n- тревога 90/+0\n- раздражение 65/+0\n"
    additions = {"привязанность": 88, "сексуальное влечение": 80,
                 "усталость от её состояния": 75, "ответственность": 90}
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        template = base / "templates"
        for name in STATE_FILES:
            p = template / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("Готовое состояние", encoding="utf-8")
        self.store = SessionStore(base / "data", template)
        self.meta = self.store.create_session("Оригинал 1:1\n", "Канон", "POV")
        self.sid = self.meta["session_id"]
        self.path = "characters/NPC_001/relations.md"
        self.basis = "Начальная оценка игровой механики по подтверждённым отношениям"

    def file(self, name, meta=None):
        meta = meta or self.store.read_state(self.sid)
        return self.store._revision(self.store._root(self.sid), meta["version"]) / name

    def seed_legacy(self):
        # Old versions accepted prose-only relation cards.
        for name in CHAR_FILES:
            _atomic_write(self.file(f"characters/NPC_001/{name}"),
                          "Давний знакомый, проявляет интерес." if name == "relations.md" else "Канон")

    def start(self, numeric=True):
        self.seed_legacy()
        if numeric:
            self.meta = self.store.update_setup(self.sid, self.meta["version"], {
                self.path: "Описание отношений.\n- интерес 50/+0\n- доверие 30/+0\n",
            })
            self.meta = self.store.activate(self.sid, self.meta["version"])
        else:
            self.meta = self.store._write_revision(
                self.store._root(self.sid), self.meta, {}, status="ready"
            )

    def save(self, relation=None, reader="Действие"):
        packet = self.store.prepare_turn(self.sid, [])
        for i in range(packet["count"]):
            self.store.read_chunk(packet["packet_id"], i)
        files = {"state/scene.md": "Сцена после хода"}
        if relation is not None:
            files[self.path] = relation
        return reviewed_commit(self.store,
            self.sid, self.meta["version"], reader, "Ответ сцены", files,
            timeline_events=[], timeline_no_change_reason="Обычный разговор без новых событий",
            packet_id=packet["packet_id"],
        )

    def test_missing_duplicate_and_other_npc_numbers_are_not_valid_metrics(self):
        for body in (
            "Только дружеская симпатия словами.",
            "## К другим НПС\n- доверие 50/+0",
            "- интерес 50/+0\n- ИНТЕРЕС 60/+0",
        ):
            with self.subTest(body=body), self.assertRaises(StateError):
                _validate_relation(body, self.path)

    def test_bad_legacy_setup_blocks_launch_even_with_approved_gaps(self):
        self.seed_legacy()
        report = self.store.setup_report(self.sid)
        self.assertEqual(report["npc_ids"], ["NPC_001"])
        self.assertTrue(report["relation_errors"])
        self.assertFalse(report["ready_without_approval"])
        with self.assertRaisesRegex(StateError, "числовые отношения"):
            self.store.activate(self.sid, self.meta["version"], True)
        with self.assertRaisesRegex(StateError, "Нет числовых"):
            self.store.update_setup(self.sid, self.meta["version"], {self.path: "Описание без шкал"})

    def test_gameplay_cannot_erase_a_scale_or_lie_about_delta(self):
        self.start()
        before = self.store.read_state(self.sid)
        for body in (
            "Теперь только словами.",
            "- интерес 50/+0",
            "- интерес 52/+1\n- доверие 30/+0",
        ):
            with self.subTest(body=body), self.assertRaises(StateError):
                self.save(body, reader="Новый тестовый ввод: " + body)
            self.assertEqual(self.store.read_state(self.sid), before)
        self.meta = self.save("- интерес 52/+2\n- доверие 30/+0")
        self.assertEqual(self.meta["turn"], 1)
        self.assertIn("интерес 52/+2", self.file(self.path).read_text())

    def test_initialize_preserves_prose_original_and_transcript_without_turn(self):
        self.start(numeric=False)
        self.meta = self.save()
        before = self.meta
        prose = self.file(self.path).read_text()
        journal = self.file("system/turns.jsonl").read_bytes()
        result = self.store.initialize_relationship_metrics(
            self.sid, before["version"], {"NPC_001": {"интерес": 65, "доверие": 20}}, self.basis
        )
        self.assertEqual(result["turn"], before["turn"])
        self.assertEqual(result["status"], before["status"])
        self.assertEqual(result["version"], before["version"] + 1)
        self.assertEqual(result["metrics_sources"]["NPC_001"]["kind"], "initialized")
        self.assertFalse(result["gameplay_turn_added"])
        self.assertTrue(self.file(self.path).read_text().endswith(prose))
        self.assertIn("интерес 65/+0", self.file(self.path).read_text())
        self.assertEqual(self.file("system/turns.jsonl").read_bytes(), journal)
        self.assertEqual(self.store.read_original(self.sid), "Оригинал 1:1\n")
        repeat = self.store.initialize_relationship_metrics(
            self.sid, result["version"], {"NPC_001": {"интерес": 65, "доверие": 20}}, self.basis
        )
        self.assertTrue(repeat["already_present"])
        self.assertEqual(repeat["version"], result["version"])
        with self.assertRaisesRegex(StateError, "уже существуют"):
            self.store.initialize_relationship_metrics(
                self.sid, result["version"], {"NPC_001": {"интерес": 99}}, self.basis
            )
        self.assertEqual(self.store.read_state(self.sid)["version"], result["version"])

    def test_historical_numbers_take_precedence_over_new_estimates(self):
        self.start()
        self.meta = self.save()
        _atomic_write(self.file(self.path), "Описание после старой ошибки без чисел.")
        with self.assertRaisesRegex(StateError, "Найдены прежние значения"):
            self.store.initialize_relationship_metrics(
                self.sid, self.meta["version"], {"NPC_001": {"интерес": 99}}, self.basis
            )
        result = self.store.initialize_relationship_metrics(
            self.sid, self.meta["version"], {"NPC_001": {"интерес": 50, "доверие": 30}}, self.basis
        )
        self.assertEqual(result["metrics_sources"]["NPC_001"]["kind"], "restored")
        self.assertIn("source_revision", result["metrics_sources"]["NPC_001"])
        self.assertEqual(result["turn"], 1)

    def test_invalid_or_stale_batch_is_atomic(self):
        self.start(numeric=False)
        before = self.meta
        for metrics, version in (
            ({"NPC_001": {"интерес": 50}}, before["version"] - 1),
            ({"NPC_001": {"интерес": 101}}, before["version"]),
            ({"NPC_001": {"интерес": True}}, before["version"]),
            ({"NPC_001": {"интерес": 50}, "NPC_002": {"доверие": 20}}, before["version"]),
        ):
            with self.subTest(metrics=metrics), self.assertRaises(StateError):
                self.store.initialize_relationship_metrics(self.sid, version, metrics, self.basis)
            self.assertEqual(self.store.read_state(self.sid), before)
            self.assertNotIn("/+0", self.file(self.path).read_text())

    def partial_author_setup(self):
        self.seed_legacy()
        _atomic_write(self.file("characters/NPC_001/core.md"), self.declared_core)
        _atomic_write(self.file(self.path), self.partial_relations)

    def test_setup_rejects_three_of_seven_even_with_approved_gaps(self):
        self.partial_author_setup()
        report = self.store.setup_report(self.sid)
        self.assertTrue(any("из 7" in issue and "ответственность" in issue for issue in report["relation_errors"]))
        with self.assertRaisesRegex(StateError, "числовые отношения"):
            self.store.activate(self.sid, self.meta["version"], True)

    def test_complete_explicit_author_scales_preserves_existing_values_and_history(self):
        self.partial_author_setup()
        self.meta = self.store._write_revision(self.store._root(self.sid), self.meta, {}, status="active", turn=10)
        before = self.meta
        journal = self.file("system/turns.jsonl").read_bytes()
        original = self.store.read_original(self.sid)
        result = self.store.initialize_relationship_metrics(
            self.sid, before["version"], {"NPC_001": self.additions}, self.basis
        )
        stats = _relation_stats(self.file(self.path).read_text(), self.path)
        self.assertEqual(len(stats), 7)
        self.assertEqual({key: stats[key] for key in ("любовь", "тревога", "раздражение")},
                         {"любовь": (85, 0), "тревога": (90, 0), "раздражение": (65, 0)})
        self.assertTrue(self.file(self.path).read_text().endswith(self.partial_relations))
        self.assertEqual(result["turn"], 10)
        self.assertEqual(result["version"], before["version"] + 1)
        self.assertFalse(result["gameplay_turn_added"])
        self.assertEqual(self.file("system/turns.jsonl").read_bytes(), journal)
        self.assertEqual(self.store.read_original(self.sid), original)
        self.assertEqual(result["metrics_sources"]["NPC_001"]["kind"], "completed")
        self.assertEqual(self.store.setup_report(self.sid)["relation_errors"], [])
        repeated = self.store.initialize_relationship_metrics(self.sid, result["version"], {"NPC_001": self.additions}, self.basis)
        self.assertEqual(repeated["version"], result["version"])

    def test_completion_cannot_invent_or_overwrite_scales_and_is_atomic(self):
        self.partial_author_setup()
        before = self.store.read_state(self.sid)
        for metrics in ({"любовь": 99}, {"интерес": 50}, {"ответственность": 90}):
            with self.subTest(metrics=metrics), self.assertRaises(StateError):
                self.store.initialize_relationship_metrics(self.sid, before["version"], {"NPC_001": metrics}, self.basis)
            self.assertEqual(self.store.read_state(self.sid), before)
            self.assertEqual(self.file(self.path).read_text(), self.partial_relations)

    def test_completion_prefers_historical_values_for_missing_scales(self):
        self.partial_author_setup()
        first = self.store.initialize_relationship_metrics(self.sid, self.meta["version"], {"NPC_001": self.additions}, self.basis)
        self.meta = self.store._write_revision(self.store._root(self.sid), first, {self.path: self.partial_relations})
        wrong = {**self.additions, "привязанность": 10}
        with self.assertRaisesRegex(StateError, "Найдено прежнее значение"):
            self.store.initialize_relationship_metrics(self.sid, self.meta["version"], {"NPC_001": wrong}, self.basis)
        right = self.store.initialize_relationship_metrics(self.sid, self.meta["version"], {"NPC_001": self.additions}, self.basis)
        self.assertEqual(len(right["metrics_sources"]["NPC_001"]["restored_from"]), 4)

    def test_no_numeric_relationship_file_for_an_npc_without_a_card(self):
        with self.assertRaisesRegex(StateError, "нет карточки"):
            _validate_relation_updates(self.store._revision(self.store._root(self.sid), self.meta["version"]),
                                       {"characters/NPC_GUARD/relations.md": "- раздражение 16/+1"})


if __name__ == "__main__":
    unittest.main()
