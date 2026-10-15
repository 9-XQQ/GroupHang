"""Synthetic task evaluation integrity and orchestration fault boundaries."""
import argparse
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.routers import discovery as router
from app.schemas import DiscoveryRequest
from scripts import evaluate_discovery as evaluation


class DiscoveryEvaluationTests(unittest.IsolatedAsyncioTestCase):
    def test_frozen_dataset_is_separate_balanced_and_multiturn(self):
        data, fingerprint = evaluation.load_tasks(evaluation.ROOT / "evals/discovery_tasks.json")
        old = json.loads((evaluation.ROOT / "evals/agent_cases.json").read_text(encoding="utf-8"))
        self.assertFalse({case["id"] for case in old["cases"]} & {c["id"] for c in data["episodes"]})
        self.assertEqual(len(fingerprint), 64)
        self.assertEqual(len(data["episodes"]), 12)
        self.assertEqual(sum(len(c["turns"]) for c in data["episodes"]), 24)
        for scenario in ("meal", "brand", "afternoon"):
            self.assertEqual(sum(c["scenario"] == scenario for c in data["episodes"]), 4)

    def test_empty_duplicate_and_unknown_expectations_are_rejected(self):
        data, _ = evaluation.load_tasks(evaluation.ROOT / "evals/discovery_tasks.json")
        variants = [{**data, "episodes": []}, {**data, "episodes": [data["episodes"][0]] * 2}]
        unknown = deepcopy(data)
        unknown["episodes"][0]["turns"][0]["expected"] = {"typo": True}
        variants.append(unknown)
        unknown = deepcopy(data)
        unknown["episodes"][0]["turns"][0]["tools"] = {"typo": True}
        variants.append(unknown)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            for variant in variants:
                path.write_text(json.dumps(variant), encoding="utf-8")
                with self.assertRaises(ValueError):
                    evaluation.load_tasks(path)

    def test_semantic_checks_fail_closed_on_empty_wrong_and_private_outputs(self):
        empty = {"saved": False, "recommendations": []}
        for expected in ({"top": "expected"}, {"min_recommendations": 1}, {"brand_only": "brand"},
                         {"estimated": True}, {"finish_within": True}, {"return_leg": True}):
            self.assertFalse(all(evaluation.score_turn(empty, expected).values()))
        result = {"saved": True, "recommendations": [{"place": {"name": "wrong"}, "estimated": False}],
                  "warnings": [evaluation.PRIVATE_MARKER]}
        checks = evaluation.score_turn(result, {"names": ["expected"], "estimated": True})
        self.assertFalse(checks["no_private_note"])
        self.assertFalse(checks["preview_only"])
        self.assertFalse(checks["names"])
        self.assertFalse(checks["estimated"])

    async def test_database_requires_explicit_opt_in(self):
        with patch.dict(os.environ, {"AGENT_DB_TESTS": "0"}), patch.object(evaluation, "create_async_engine") as engine:
            with self.assertRaises(ValueError):
                async with evaluation.isolated_sessions():
                    self.fail("must not enter")
            engine.assert_not_called()

    async def test_tool_timeout_rolls_back_and_returns_504(self):
        db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(input_version=1)),
                             rollback=AsyncMock(), commit=AsyncMock())
        with patch.object(router, "require_trip_member", new=AsyncMock()), patch.object(
                router, "discover", new=AsyncMock(side_effect=TimeoutError("private text"))):
            with self.assertRaises(HTTPException) as caught:
                await router.run_discovery(1, DiscoveryRequest(scenario="meal"), None, db)
        self.assertEqual(caught.exception.status_code, 504)
        self.assertNotIn("private", caught.exception.detail)
        db.rollback.assert_awaited_once()
        db.commit.assert_not_awaited()

    async def test_input_change_during_tools_rejects_stale_recommendation(self):
        db = SimpleNamespace(get=AsyncMock(side_effect=[SimpleNamespace(input_version=1), SimpleNamespace(input_version=2)]),
                             rollback=AsyncMock(), commit=AsyncMock())
        with patch.object(router, "require_trip_member", new=AsyncMock()), patch.object(
                router, "discover", new=AsyncMock(return_value={"recommendations": []})):
            with self.assertRaises(HTTPException) as caught:
                await router.run_discovery(1, DiscoveryRequest(scenario="meal"), None, db)
        self.assertEqual(caught.exception.status_code, 409)


@unittest.skipUnless(os.getenv("AGENT_DB_TESTS") == "1", "set AGENT_DB_TESTS=1")
class DiscoveryTaskDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_task_run_is_offline_private_and_removes_schema(self):
        # Capture exact schema creation/drop statements, without recording params/URLs.
        from sqlalchemy import event
        statements = []
        original_engine = evaluation.create_async_engine
        def tracked_engine(*args, **kwargs):
            engine = original_engine(*args, **kwargs)
            def record(conn, cursor, statement, parameters, context, executemany):
                if statement.startswith(('CREATE SCHEMA "discovery_eval_', 'DROP SCHEMA "discovery_eval_')):
                    statements.append(statement)
            event.listen(engine.sync_engine, "before_cursor_execute", record)
            return engine
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(cases=evaluation.ROOT / "evals/discovery_tasks.json", output_dir=Path(directory))
            with patch.object(evaluation, "create_async_engine", tracked_engine), patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(await evaluation.run(args), 0)
            report = json.loads((Path(directory) / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["metrics"]["passed_episodes"], 12)
            self.assertEqual(report["metrics"]["passed_turns"], 24)
            self.assertEqual(report["real_model_calls"], 0)
            self.assertEqual(report["real_map_calls"], 0)
            self.assertTrue(report["network_disabled"])
            serialized = json.dumps(report, ensure_ascii=False)
            for forbidden in (evaluation.PRIVATE_MARKER, "121.42", "合成甲", "start_location", "合成评测行程"):
                self.assertNotIn(forbidden, serialized)
            self.assertEqual(len(statements), 2)
            self.assertEqual(statements[0].split('"')[1], statements[1].split('"')[1])

    async def test_unexpected_task_output_fails_gate_and_records_checks(self):
        data, _ = evaluation.load_tasks(evaluation.ROOT / "evals/discovery_tasks.json")
        wrong = deepcopy(data["episodes"][1])
        wrong["turns"][0]["expected"]["top"] = "故意错误的期望"
        async with evaluation.isolated_sessions() as sessions:
            result = await evaluation.evaluate_episode(sessions, wrong)
        self.assertFalse(result["passed"])
        self.assertFalse(result["turns"][0]["checks"]["top"])
        self.assertTrue(result["turns"][1]["passed"])
