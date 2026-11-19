"""Security and contract tests for the server-owned independent scene reviewer."""
import json
import os
import unittest
from unittest.mock import patch

from chatnovella.semantic_review import (
    SEMANTIC_KEYS, ReviewUnavailable, evaluate_semantic, validate_verdict,
)


def verdict():
    return {
        "findings": {
            key: {
                "status": "na" if key in ("scene_depth", "npc_character", "relationships") else "pass",
                "evidence": "Содержательная проверка на исходном тексте и карточках NPC.",
                "fix": "",
            } for key in SEMANTIC_KEYS
        },
        "summary": "Серверная проверка завершилась с доказательствами по каждому пункту.",
    }


class TestSemanticReview(unittest.TestCase):
    def test_missing_key_fails_closed(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with self.assertRaisesRegex(ReviewUnavailable, "OPENAI_API_KEY"):
                evaluate_semantic({"draft_scene": "Тест"})

    def test_reject_missing_sections_and_fake_na(self):
        data = verdict()
        del data["findings"]["plot_progress"]
        with self.assertRaises(ReviewUnavailable):
            validate_verdict(data)
        data = verdict()
        data["findings"]["plot_progress"]["status"] = "na"
        with self.assertRaises(ReviewUnavailable):
            validate_verdict(data)

    def test_failure_requires_actionable_feedback(self):
        data = verdict()
        data["findings"]["npc_initiative"] = {
            "status": "fail", "evidence": "Ни один NPC не сделал ничего значимого.",
            "fix": "",
        }
        with self.assertRaises(ReviewUnavailable):
            validate_verdict(data)

    def test_calls_separate_api_with_strict_schema(self):
        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self, _count):
                return json.dumps({"choices": [{"finish_reason": "stop",
                    "message": {"content": json.dumps(verdict(), ensure_ascii=False)}}]},
                    ensure_ascii=False).encode()
        def mock_urlopen(request, timeout):
            self.assertEqual(timeout, 90)
            self.assertEqual(request.get_header("Authorization"), "Bearer local-test-secret")
            body = json.loads(request.data)
            self.assertEqual(body["response_format"]["type"], "json_schema")
            self.assertTrue(body["response_format"]["json_schema"]["strict"])
            self.assertIn("draft_scene", body["messages"][1]["content"])
            return FakeResponse()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "local-test-secret"}), \
             patch("chatnovella.semantic_review.urlopen", side_effect=mock_urlopen):
            result = evaluate_semantic({"draft_scene": "Черновик"})
        self.assertEqual(set(result["findings"]), set(SEMANTIC_KEYS))


if __name__ == "__main__":
    unittest.main()
