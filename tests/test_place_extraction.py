"""P0-A contract tests. No database, migrations or external HTTP requests."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException

from app.db import get_db
from app.routers import shared_text as route
from app.schemas import SharedTextParseRequest
from app.security import get_current_user
from app.services.llm_parser import LlmPlaceParser, MAX_EXTRACTION_INPUT


def model_response(content, status=200, api_style="openai"):
    payload = ({"content": [{"type": "text", "text": content}]} if api_style == "anthropic" else
               {"choices": [{"message": {"content": content}}]})
    payload["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    return httpx.Response(status, json=payload)


class OfflineExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.network_guard = patch("httpx.AsyncHTTPTransport.handle_async_request",
                                   new=AsyncMock(side_effect=AssertionError("External network forbidden")))
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)
        self.parser = LlmPlaceParser()
        self.parser.api_key = "synthetic-test-key"
        self.parser.model = "synthetic-test-model"
        self.parser.base_url = "https://synthetic.invalid/v1"
        self.content = '{"places":[]}'
        self.status = 200
        self.exception = None
        self.requests = []

        async def handler(request):
            self.requests.append(json.loads(request.content))
            if self.exception:
                raise self.exception
            return model_response(self.content, self.status, self.parser._api_style())
        self.parser._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(self.parser.aclose)

    def payload(self, places):
        self.content = json.dumps({"places": places}, ensure_ascii=False)


class PlaceExtractionContractTests(OfflineExtractionTests):
    async def test_single_place_keeps_grounded_fields_and_legacy_tuple(self):
        source = "上海市的演示餐厅，地址是示例路1号，餐饮，人均50元，理由：方便集合。"
        self.payload([{"name": "演示餐厅", "city": "上海市", "address": "示例路1号",
                       "category": "餐饮", "price": "50元", "reason": "方便集合", "source_excerpt": source}])
        places, error = await self.parser.parse(source)
        self.assertIsNone(error)
        self.assertIsNone(self.parser.last_parse_error_code)
        self.assertEqual(places[0]["address"], "示例路1号")
        self.assertEqual(places[0]["source_excerpt"], source)
        self.assertEqual(self.parser.last_parse_warnings, [])
        self.assertEqual(self.parser.last_usage["total_tokens"], 15)
        self.assertEqual(len(self.requests), 1)

    async def test_multiple_places_and_optional_fields(self):
        self.payload([{"name": "演示公园"}, {"name": "演示美术馆"}])
        places, error = await self.parser.parse("去演示公园，再去演示美术馆。")
        self.assertIsNone(error)
        self.assertEqual([p["name"] for p in places], ["演示公园", "演示美术馆"])
        self.assertTrue(all(p["address"] == "" for p in places))

    async def test_valid_no_place_is_empty_success(self):
        places, error = await self.parser.parse("今天想休息，预算一百元，不知道去哪里。")
        self.assertEqual(places, [])
        self.assertIsNone(error)
        self.assertIsNone(self.parser.last_parse_error_code)

    async def test_missing_places_is_not_empty_success(self):
        for content in ('{}', '{"reply":"没有地点"}'):
            self.content = content
            places, error = await self.parser.parse("今天只想休息。")
            self.assertEqual(places, [])
            self.assertIsNotNone(error)
            self.assertEqual(self.parser.last_parse_error_code, "invalid_schema")

    async def test_invalid_json_and_duplicate_keys(self):
        for content in ("not json SECRET", '{"places":', '{"places":[],"places":[]}', '{"places":NaN}'):
            self.content = content
            _, error = await self.parser.parse("演示公园")
            self.assertEqual(self.parser.last_parse_error_code, "invalid_json")
            self.assertNotIn("SECRET", error)

    async def test_wrong_schema_and_unknown_fields_are_rejected(self):
        for content in ('[]', '{"places":null}', '{"places":{}}', '{"places":[{}]}',
                        '{"places":[{"name":3}]}', '{"places":[{"name":"演示公园","address":null}]}',
                        '{"places":[],"unexpected":true}', '{"places":[{"name":"演示公园","lat":31}]}'):
            with self.subTest(content=content):
                self.content = content
                _, error = await self.parser.parse("演示公园")
                self.assertIsNotNone(error)
                self.assertEqual(self.parser.last_parse_error_code, "invalid_schema")

    async def test_place_count_and_blank_name_are_bounded(self):
        for places in ([{"name": "演示公园"}] * 11, [{"name": " \n "}]):
            self.payload(places)
            _, error = await self.parser.parse("演示公园")
            self.assertIsNotNone(error)
            self.assertEqual(self.parser.last_parse_error_code, "invalid_schema")

    async def test_unsupported_address_and_other_fields_are_removed(self):
        self.payload([{"name": "演示公园", "address": "虚构路999号", "city": "虚构市",
                       "price": "免费", "category": "景点", "reason": "适合亲子"}])
        places, error = await self.parser.parse("想去演示公园。")
        self.assertIsNone(error)
        for field in ("address", "city", "price", "category", "reason"):
            self.assertEqual(places[0][field], "")
            self.assertIn(f"ungrounded_{field}_removed", self.parser.last_parse_warnings)

    async def test_unsupported_name_is_dropped_without_system_failure(self):
        self.payload([{"name": "根本未提到的餐厅", "address": "某路1号"}])
        places, error = await self.parser.parse("今天不知道去哪，先休息。")
        self.assertEqual(places, [])
        self.assertIsNone(error)
        self.assertEqual(self.parser.last_parse_warnings, ["ungrounded_name_dropped"])

    async def test_excerpt_must_be_grounded_and_narrows_address_scope(self):
        self.payload([{"name": "演示公园", "address": "美术路2号", "source_excerpt": "演示公园在公园路1号"},
                      {"name": "演示美术馆", "source_excerpt": "模型编造的片段"}])
        places, _ = await self.parser.parse("演示公园在公园路1号；演示美术馆在美术路2号。")
        self.assertEqual(places[0]["address"], "")
        self.assertEqual(places[1]["source_excerpt"], "")
        self.assertIn("ungrounded_source_excerpt_removed", self.parser.last_parse_warnings)

    async def test_evidence_normalizes_width_case_and_whitespace(self):
        self.payload([{"name": "ABC Cafe", "address": "示例路1号"}])
        places, error = await self.parser.parse("ＡＢＣ　Ｃａｆｅ 地址：示例路１号")
        self.assertIsNone(error)
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["address"], "示例路1号")

    async def test_unsupported_values_do_not_leak_in_metadata(self):
        self.payload([{"name": "演示公园", "address": "SECRET_ADDRESS", "source_excerpt": "SECRET_EXCERPT"}])
        places, error = await self.parser.parse("演示公园")
        serialized = json.dumps({"places": places, "error": error, "warnings": self.parser.last_parse_warnings})
        self.assertNotIn("SECRET", serialized)

    async def test_errors_are_stable_and_reset_after_success(self):
        for status, code in ((429, "rate_limited"), (500, "service_unavailable"), (503, "service_unavailable"),
                             (401, "authentication_failed"), (403, "authentication_failed"), (400, "http_error")):
            self.status = status
            _, error = await self.parser.parse("演示公园")
            self.assertEqual(self.parser.last_parse_error_code, code)
            self.assertNotIn("synthetic.invalid", error)
            self.assertEqual(len(self.requests), list((429, 500, 503, 401, 403, 400)).index(status) + 1)
        self.status = 200
        self.assertEqual(await self.parser.parse("今天休息"), ([], None))
        self.assertIsNone(self.parser.last_parse_error_code)

    async def test_timeout_network_and_no_retries(self):
        for exc, code in ((httpx.ReadTimeout("SECRET"), "timeout"), (httpx.ConnectTimeout("SECRET"), "timeout"),
                          (httpx.ConnectError("SECRET"), "network_error")):
            self.exception = exc
            before = len(self.requests)
            _, error = await self.parser.parse("演示公园")
            self.assertEqual(self.parser.last_parse_error_code, code)
            self.assertNotIn("SECRET", error)
            self.assertEqual(len(self.requests), before + 1)

    async def test_overall_timeout(self):
        async def slow(*args, **kwargs):
            await asyncio.sleep(1)
        self.parser.timeout = .01
        with patch.object(self.parser._client, "post", side_effect=slow):
            _, error = await self.parser.parse("演示公园")
        self.assertIsNotNone(error)
        self.assertEqual(self.parser.last_parse_error_code, "timeout")

    async def test_invalid_inputs_and_unconfigured_do_not_send_requests(self):
        for value, code in ((None, "invalid_input"), (" \n", "input_empty"), ("字" * (MAX_EXTRACTION_INPUT + 1), "input_too_long")):
            _, error = await self.parser.parse(value)
            self.assertIsNotNone(error)
            self.assertEqual(self.parser.last_parse_error_code, code)
        self.parser.api_key = ""
        await self.parser.parse("演示公园")
        self.assertEqual(self.parser.last_parse_error_code, "not_configured")
        self.assertEqual(self.requests, [])

    async def test_long_valid_input_is_not_silently_truncated(self):
        self.payload([{"name": "演示公园"}])
        source = "普通内容" * 1600 + "最后去演示公园。"
        places, _ = await self.parser.parse(source)
        self.assertEqual(places[0]["name"], "演示公园")
        self.assertIn(source, self.requests[0]["messages"][-1]["content"])

    async def test_fenced_json_and_anthropic_compatibility(self):
        self.parser.base_url = "https://synthetic.invalid/anthropic"
        self.content = '```json\n{"places":[{"name":"演示公园"}]}\n```'
        places, error = await self.parser.parse("演示公园")
        self.assertIsNone(error)
        self.assertEqual(places[0]["name"], "演示公园")

    async def test_malformed_provider_envelope_is_structured_failure(self):
        for payload in ({}, [], {"choices": []}, {"choices": [{"message": {"content": None}}]}):
            async def handler(request):
                return httpx.Response(200, json=payload)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(self.parser, "_http_client", return_value=client):
                    _, error = await self.parser.parse("演示公园")
            self.assertIsNotNone(error)
            self.assertEqual(self.parser.last_parse_error_code, "invalid_response")

    async def test_request_local_error_and_warning_metadata_is_isolated(self):
        self.payload([{"name": "演示公园", "address": "虚构地址"}])
        async def run(source):
            places, error = await self.parser.parse(source)
            return places, error, self.parser.last_parse_error_code, self.parser.last_parse_warnings
        bad, good = await asyncio.gather(run(""), run("演示公园"))
        self.assertEqual(bad[2], "input_empty")
        self.assertEqual(bad[3], [])
        self.assertIsNone(good[2])
        self.assertEqual(good[3], ["ungrounded_address_removed"])


class SharedTextExtractionRouteTests(OfflineExtractionTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.patches = [patch.object(route, "llm_place_parser", self.parser),
                        patch.object(route, "require_trip_member", new=AsyncMock()),
                        patch.object(route.amap, "search_pois_verified", new=AsyncMock(return_value={
                            "status": "not_found", "candidates": [], "selected": None,
                            "error_code": None, "message": None, "complete": True, "source": "poi"})),
                        patch.object(route.amap, "geocode_verified", new=AsyncMock(return_value={
                            "status": "not_found", "candidates": [], "selected": None,
                            "error_code": None, "message": None, "complete": True, "source": "geocode"})),
                        patch.object(route.amap, "resolve_amap_share_url", new=AsyncMock(return_value=None))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.app = FastAPI()
        self.app.include_router(route.router)
        async def no_write_db():
            yield SimpleNamespace()  # No ORM or database connection; writes would fail.
        self.app.dependency_overrides[get_db] = no_write_db
        self.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
        self.api = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.addAsyncCleanup(self.api.aclose)

    async def call(self, text, **updates):
        response = await self.api.post("/trips/1/shared-text/parse", json={"text": text, "use_llm": True, **updates})
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def test_empty_success_does_not_fall_back_or_call_map(self):
        result = await self.call("今天只想休息，预算一百元。")
        self.assertEqual(result["candidates"], [])
        self.assertTrue(result["llm_used"])
        self.assertEqual(result["llm_status"], "empty")
        self.assertIsNone(result["llm_error_code"])
        route.amap.search_pois_verified.assert_not_awaited()
        route.amap.geocode_verified.assert_not_awaited()

    async def test_empty_llm_preserves_independently_resolved_share_link(self):
        route.amap.resolve_amap_share_url.return_value = {
            "name": "分享地点", "address": "合成地址", "lat": 31.2, "lng": 121.4}
        result = await self.call("https://uri.amap.com/marker?position=121.4,31.2")
        self.assertEqual(result["llm_status"], "empty")
        self.assertTrue(result["llm_used"])
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["resolution_source"], "amap_share_url")
        route.amap.search_pois_verified.assert_not_awaited()
        route.amap.geocode_verified.assert_not_awaited()

    async def test_only_unsupported_names_is_empty_without_rule_fallback(self):
        self.payload([{"name": "模型猜测的地点"}])
        result = await self.call("今天就休息吧，没有计划。")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["llm_status"], "empty")
        self.assertIsNone(result["llm_error_code"])
        self.assertEqual(result["llm_warnings"], ["ungrounded_name_dropped"])
        route.amap.search_pois_verified.assert_not_awaited()
        route.amap.geocode_verified.assert_not_awaited()

    async def test_success_keeps_preview_response_and_grounded_address_reaches_map(self):
        self.payload([{"name": "演示公园", "address": "示例路1号", "city": "上海市"}])
        result = await self.call("上海市演示公园在示例路1号。")
        self.assertEqual(result["llm_status"], "success")
        for field in ("candidates", "warnings", "parser", "parse_session_id", "source_platform", "comment_summary", "source_adapter", "llm_requested", "llm_used", "llm_usage"):
            self.assertIn(field, result)
        route.amap.geocode_verified.assert_awaited_once_with("上海市示例路1号")

    async def test_unsupported_address_never_reaches_geocode(self):
        self.payload([{"name": "演示公园", "address": "虚构路999号", "city": "虚构市"}])
        result = await self.call("去演示公园。")
        self.assertEqual(result["candidates"][0]["address"], "")
        self.assertIn("ungrounded_address_removed", result["llm_warnings"])
        self.assertNotIn("虚构路", json.dumps(result, ensure_ascii=False))
        route.amap.geocode_verified.assert_not_awaited()
        route.amap.search_pois_verified.assert_not_awaited()

    async def test_failed_model_falls_back_with_stable_error_and_rule_output(self):
        text = "演示公园｜示例路1号｜公园｜免费｜散步"
        for content in ('{}', 'not JSON'):
            self.content = content
            result = await self.call(text)
            self.assertFalse(result["llm_used"])
            self.assertEqual(result["llm_status"], "failed")
            self.assertIn(result["llm_error_code"], ("invalid_schema", "invalid_json"))
            self.assertEqual(result["candidates"][0]["name"], "演示公园")
            self.assertTrue(any("已使用规则解析" in w for w in result["warnings"]))

    async def test_use_llm_false_keeps_original_rules_without_model_request(self):
        result = await self.call("演示公园｜示例路1号｜公园｜免费｜散步", use_llm=False)
        self.assertEqual(result["llm_status"], "not_requested")
        self.assertFalse(result["llm_used"])
        self.assertIsNone(result["llm_error_code"])
        self.assertEqual(result["llm_warnings"], [])
        self.assertEqual(result["candidates"][0]["name"], "演示公园")
        self.assertEqual(result["candidates"][0]["address"], "示例路1号")
        self.assertEqual(result["parser"], "rules_v1+amap_share_v1")
        self.assertEqual(self.requests, [])

    async def test_comments_are_user_evidence_but_server_heading_is_not(self):
        self.payload([{"name": "演示公园"}, {"name": "用户粘贴的评论"}])
        result = await self.call("今天聊一下行程。", comments_text="演示公园值得去。")
        self.assertEqual([p["name"] for p in result["candidates"]], ["演示公园"])
        self.assertIn("ungrounded_name_dropped", result["llm_warnings"])

    async def test_http_error_classification_and_request_validation(self):
        self.status = 429
        result = await self.call("演示公园")
        self.assertEqual(result["llm_error_code"], "rate_limited")
        for text in ("", "字" * 10001):
            response = await self.api.post("/trips/1/shared-text/parse", json={"text": text, "use_llm": True})
            self.assertEqual(response.status_code, 422)

    async def test_member_rejection_happens_before_model_and_map(self):
        route.require_trip_member.side_effect = HTTPException(403, "not a member")
        response = await self.api.post("/trips/1/shared-text/parse", json={"text": "演示公园", "use_llm": True})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.requests, [])
        route.amap.geocode_verified.assert_not_awaited()
