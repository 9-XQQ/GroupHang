"""Payload-free tracing, isolation and access-control regressions."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException

from app.services import agent_trace as trace
from app.services.llm_parser import LlmPlaceParser
from app.routers import agent


class AgentTraceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        trace._traces.clear()
        self.user = SimpleNamespace(id=901)

    def tearDown(self):
        trace._traces.clear()

    async def test_concurrent_requests_nested_spans_and_no_payloads(self):
        @trace.traced_stage("map.request")
        async def tool(secret):
            await asyncio.sleep(0)
            return {"key": secret, "lat": 31.2}

        @trace.traced_stage("execution.prepare")
        async def prepare(secret):
            return await tool(secret)

        @trace.traced_request("execute")
        async def request(trip_id, user, secret):
            await prepare(secret)
            return {"replayed": True, "reply": secret}

        results = await asyncio.gather(*[
            request(i, self.user, "private-prompt-key-location") for i in (1, 2)])
        self.assertNotEqual(results[0]["trace_id"], results[1]["trace_id"])
        for i, result in enumerate(results, 1):
            record = trace.read_traces(self.user.id, i, result["trace_id"])[0]
            self.assertEqual(len(record["spans"]), 2)
            self.assertEqual(record["spans"][1]["parent_id"], 1)
            self.assertTrue(record["replayed"])
            self.assertNotIn("private-prompt", json.dumps(record))
        self.assertIsNone(trace._current.get())

    async def test_real_model_wrapper_timeout_is_safe_and_fallback_is_explicit(self):
        parser = LlmPlaceParser()
        parser.api_key, parser.model = "synthetic-key", "synthetic-model"
        client = SimpleNamespace(post=AsyncMock(side_effect=httpx.ReadTimeout("secret URL key")))

        @trace.traced_stage("assistant.decide", outcome="assistant")
        async def decide():
            result, warning = await parser.complete_json("secret system", "secret prompt")
            self.assertIsNone(result)
            return {}, False, warning

        @trace.traced_request("chat")
        async def request(trip_id, user):
            await decide()
            return {"llm_used": False}

        with patch.object(parser, "_http_client", return_value=client):
            result = await request(7, self.user)
        record = trace.read_traces(901, 7, result["trace_id"])[0]
        self.assertEqual(record["spans"][0]["status"], "fallback")
        self.assertEqual(record["spans"][1]["status"], "unavailable")
        self.assertEqual(record["spans"][1]["error_type"], "ReadTimeout")
        self.assertNotIn("secret", json.dumps(record))

    async def test_error_and_telemetry_failure_do_not_change_business_result(self):
        @trace.traced_request("execute")
        async def request(trip_id, user):
            raise HTTPException(409, "private detail")
        with self.assertRaises(HTTPException) as caught:
            await request(1, self.user)
        record = trace.read_traces(901, 1)[0]
        self.assertEqual(record["http_status"], 409)
        self.assertEqual(caught.exception.headers["X-Agent-Trace-Id"], record["trace_id"])
        self.assertNotIn("private detail", json.dumps(record))
        with patch.object(trace, "_save", side_effect=RuntimeError("disk")):
            with self.assertRaises(HTTPException) as caught:
                await request(1, self.user)
        self.assertEqual(caught.exception.status_code, 409)

    async def test_limits_expiry_usage_whitelist_and_copies(self):
        @trace.traced_stage("model.complete_json")
        async def tool():
            trace.annotate_stage(usage={"input_tokens": 12, "prompt": "secret", "total_tokens": -1})

        @trace.traced_request("chat")
        async def request(trip_id, user):
            for _ in range(4):
                await tool()
            return {}

        with patch.object(trace, "MAX_PER_USER", 2), patch.object(trace, "MAX_SPANS", 2):
            for _ in range(3):
                await request(1, self.user)
        records = trace.read_traces(901, 1)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["dropped_spans"], 2)
        self.assertEqual(records[0]["spans"][0]["usage"], {"input_tokens": 12})
        records[0]["status"] = "tampered"
        self.assertEqual(trace.read_traces(901, 1)[0]["status"], "ok")
        with patch.object(trace, "monotonic", return_value=10**20):
            self.assertEqual(trace.read_traces(901, 1), [])
        with patch.object(trace, "MAX_TRACES", 1):
            await request(1, self.user)
            await request(2, SimpleNamespace(id=902))
        self.assertEqual(len(trace._traces), 1)

    async def test_route_estimate_and_cancelled_requests(self):
        from app.services.itinerary import _route
        from app.services.amap import amap

        @trace.traced_request("chat")
        async def request(trip_id, user):
            await _route((31.2, 121.4), (31.3, 121.5), "transit")
            raise asyncio.CancelledError()

        with patch.object(amap, "route_detail", new=AsyncMock(return_value=None)):
            with self.assertRaises(asyncio.CancelledError):
                await request(1, self.user)
        record = trace.read_traces(901, 1)[0]
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(record["spans"][0]["status"], "estimated")
        self.assertNotIn("121.4", json.dumps(record))
        self.assertIsNone(trace._current.get())

    async def test_trace_routes_owner_scope_and_fastapi_signature(self):
        @trace.traced_request("chat")
        async def request(trip_id, user):
            return {}
        result = await request(1, self.user)
        with patch.object(agent, "require_trip_member", new=AsyncMock()) as member:
            record = await agent.get_trace(1, result["trace_id"], self.user, None)
            self.assertEqual(record["user_id"], 901)
            with self.assertRaises(HTTPException) as denied:
                await agent.get_trace(1, result["trace_id"], SimpleNamespace(id=902), None)
            self.assertEqual(denied.exception.status_code, 404)
            with self.assertRaises(HTTPException):
                await agent.get_trace(2, result["trace_id"], self.user, None)
            self.assertEqual(member.await_count, 3)
        with patch.object(agent, "require_trip_member", new=AsyncMock(side_effect=HTTPException(403))):
            with self.assertRaises(HTTPException):
                await agent.list_traces(1, self.user, None)
        app = FastAPI()
        app.include_router(agent.router)
        parameters = app.openapi()["paths"]["/trips/{trip_id}/assistant/chat"]["post"]["parameters"]
        self.assertEqual([p["name"] for p in parameters], ["trip_id"])
