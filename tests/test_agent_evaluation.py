"""Agent evaluation must fail closed, stay offline by default, and report honestly."""
import argparse
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from scripts import evaluate_agent as evaluation


class AgentEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_dataset_gate_and_reports_exclude_prompts(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(cases=evaluation.ROOT/"evals/agent_cases.json", mode="offline",
                                      max_cases=None, category=None, output_dir=Path(directory))
            with patch("httpx.AsyncClient.send", AsyncMock(side_effect=AssertionError("No network"))), patch("sys.stdout",new=io.StringIO()):
                result = await evaluation.run(args)
            self.assertEqual(result, 0)
            report = json.loads((Path(directory)/"report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["metrics"]["cases"], 31)
            self.assertEqual(report["metrics"]["failed"], 0)
            self.assertEqual(report["metrics"]["unexpected_actions"], 0)
            self.assertEqual(report["real_model_calls"], 0)
            self.assertTrue(report["network_disabled"])
            self.assertNotIn("公开合成演示行程", json.dumps(report,ensure_ascii=False))
            self.assertNotIn("import_text", json.dumps(report))
            markdown = (Path(directory)/"report.md").read_text(encoding="utf-8")
            self.assertIn("\n| Category",markdown)
            self.assertEqual(len(report["dataset_sha256"]),64)

    def test_extra_or_duplicate_action_cannot_count_as_exact_success(self):
        score = evaluation.score_actions([], [{"type":"generate_itinerary"}])
        self.assertFalse(score["exact"])
        score = evaluation.score_actions([{"type":"compare_itineraries"}],
                                         [{"type":"compare_itineraries"},{"type":"compare_itineraries"}])
        self.assertEqual(score["matched"],1)
        self.assertFalse(score["exact"])
        self.assertFalse(evaluation.score_actions([{"type":"update_destination","destination_id":101}],
                                               [{"type":"update_destination","destination_id":999}])["exact"])

    async def test_successful_model_output_also_obeys_negative_intent(self):
        dataset,_=evaluation.load_cases(evaluation.ROOT/"evals/agent_cases.json")
        for message,action in [
            ("不要生成路线",{"type":"generate_itinerary"}),
            ("不要保存正式路线",{"type":"generate_itinerary"}),
            ("不要切换为共同约点",{"type":"switch_workflow","primary_workflow":"meeting"}),
            ("不要把演示公园停留时间改成20分钟",{"type":"update_destination","destination_id":102,"expected_stay_min":20}),
        ]:
            with self.subTest(message=message):
                case={"id":"negative_model","category":"negation","kind":"contract","message":message,
                      "model_payload":{"reply":"模型提出了不应接受的动作","actions":[action]},
                      "expected":{"actions":[],"llm_used":True}}
                result=await evaluation.evaluate_case(case,dataset["default_context"],"offline")
                self.assertTrue(result["passed"],result["checks"])

    async def test_live_fallback_is_failure_not_model_success(self):
        dataset,_=evaluation.load_cases(evaluation.ROOT/"evals/agent_cases.json")
        case=next(c for c in dataset["cases"] if c["id"]=="no_mutation_chat")
        with patch.object(evaluation.llm_place_parser,"complete_json",AsyncMock(return_value=(None,"LLM 助手失败：ReadTimeout"))):
            result=await evaluation.evaluate_case(case,dataset["default_context"],"live")
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["real_model_response"])
        self.assertEqual(result["provider_error"],"ReadTimeout")

    async def test_empty_selection_and_unbounded_live_run_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            args=argparse.Namespace(cases=evaluation.ROOT/"evals/agent_cases.json",mode="offline",
                                    max_cases=None,category="no-such-category",output_dir=Path(directory))
            with self.assertRaises(ValueError):
                await evaluation.run(args)
            args.category=None
            for limit in (0,-1):
                args.max_cases=limit
                with self.assertRaises(ValueError):
                    await evaluation.run(args)
            args.max_cases=None
            args.mode="live";args.category=None
            with patch.object(evaluation.llm_place_parser,"api_key","eval-only"),                  patch.object(evaluation.llm_place_parser,"model","eval-only"):
                with self.assertRaises(ValueError):
                    await evaluation.run(args)

    def test_duplicate_case_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            data={"default_context":{},"cases":[{"id":"duplicate","category":"validation","kind":"rules","message":"hi","expected":{"actions":[]}}]*2}
            path=Path(directory)/"cases.json";path.write_text(json.dumps(data),encoding="utf-8")
            with self.assertRaises(ValueError):
                evaluation.load_cases(path)
