"""B1 offline map evidence and preview tests; external transport is forbidden."""
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.services.amap import AmapClient


class PlaceVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.guard = patch("httpx.AsyncHTTPTransport.handle_async_request",
                           AsyncMock(side_effect=AssertionError("External network forbidden")))
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.client = AmapClient(key="synthetic-test-key")
        self.response = {"status": "1", "count": "1", "pois": [self.poi()]}
        self.requests = []

        async def handler(request):
            self.requests.append(request)
            return httpx.Response(200, json=self.response)
        self.client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(self.client.aclose)

    @staticmethod
    def poi(**changes):
        return {"id": "synthetic-poi", "name": "演示公园", "address": "示例路1号",
                "cityname": "上海市", "adname": "演示区", "location": "121.4,31.2", **changes}

    async def test_complete_unique_exact_match(self):
        result = await self.client.verify_place("演示公园", "上海市", "示例路1号")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["selected"]["poi_id"], "synthetic-poi")
        self.assertEqual(self.requests[0].url.params["citylimit"], "true")

    async def test_multiple_fuzzy_truncated_unknown_or_conflicting_results_need_selection(self):
        cases = [
            {"count": "2", "pois": [self.poi(), self.poi(id="other")]},
            {"count": "1", "pois": [self.poi(name="演示公园北门")]},
            {"count": "8", "pois": [self.poi()]},
            {"count": None, "pois": [self.poi()]},
            {"count": "1", "pois": [self.poi(cityname="其他市")]},
            {"count": "1", "pois": [self.poi(cityname="")]},
            {"count": "1", "pois": [self.poi(address="另一路2号")]},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.response = {"status": "1", **payload}
                result = await self.client.verify_place("演示公园", "上海市", "示例路1号")
                self.assertEqual(result["status"], "ambiguous")
                self.assertIsNone(result["selected"])

    async def test_no_conditions_is_unverified_without_request(self):
        result = await self.client.verify_place("演示公园")
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(self.requests, [])

    async def test_successful_empty_is_not_found(self):
        self.response = {"status": "1", "count": "0", "pois": []}
        result = await self.client.verify_place("演示公园", "上海市")
        self.assertEqual(result["status"], "not_found")
        self.assertIsNone(result["error_code"])

    async def test_failed_poi_query_never_falls_through_to_geocoding(self):
        self.response = {"status": "0", "info": "synthetic error", "infocode": "10021"}
        result = await self.client.verify_place("演示公园", "上海市", "示例路1号")
        self.assertEqual(result["status"], "verification_failed")
        self.assertEqual(result["error_code"], "quota_exceeded")
        self.assertEqual(len(self.requests), 1)

    async def test_unconfigured_and_provider_failures(self):
        self.client.available = False
        result = await self.client.search_pois_verified("演示公园", "上海市")
        self.assertEqual(result["error_code"], "not_configured")
        self.assertEqual(self.requests, [])
        self.client.available = True
        self.response = {"status": "0", "infocode": "10001", "info": "synthetic"}
        result = await self.client.search_pois_verified("演示公园", "上海市")
        self.assertEqual(result["error_code"], "provider_error")

    async def test_timeout_and_network_failure_are_distinct_from_no_results(self):
        for exc, code in [(httpx.ReadTimeout("synthetic"), "timeout"),
                          (httpx.ConnectError("synthetic"), "network_error")]:
            async def handler(request):
                raise exc
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                self.client._client = client
                with patch("app.services.amap.asyncio.sleep", new=AsyncMock()):
                    result = await self.client.search_pois_verified("演示公园", "上海市")
                self.assertEqual(result["status"], "verification_failed")
                self.assertEqual(result["error_code"], code)

    async def test_retry_success_clears_previous_failure(self):
        attempts = 0
        async def handler(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ReadTimeout("synthetic")
            return httpx.Response(200, json=self.response)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.client._client = client
            with patch("app.services.amap.asyncio.sleep", new=AsyncMock()):
                result = await self.client.verify_place("演示公园", "上海市")
            self.assertEqual(result["status"], "matched")
            self.assertIsNone(self.client.last_error)

    async def test_http_status_failures_have_stable_safe_codes(self):
        for status, code in [(429, "rate_limited"), (401, "authentication_failed"),
                             (503, "service_unavailable"), (400, "http_error")]:
            async def handler(request):
                return httpx.Response(status, text="synthetic private provider body")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                self.client._client = client
                with patch("app.services.amap.asyncio.sleep", new=AsyncMock()):
                    result = await self.client.search_pois_verified("演示公园", "上海市")
                self.assertEqual(result["error_code"], code)
                self.assertNotIn("private", str(result))

    async def test_malformed_payloads_and_invalid_coordinates_fail_closed(self):
        for payload in [[], {"status": "1", "pois": None},
                        {"status": "1", "count": "0", "pois": [self.poi()]},
                        {"status": "1", "count": "wrong", "pois": []},
                        {"status": "1", "count": "2", "pois": []},
                        *[{"status": "1", "count": "1", "pois": [self.poi(location=value)]}
                          for value in ("nan,31", "181,31", "121,91", "missing")]]:
            self.response = payload
            result = await self.client.search_pois_verified("演示公园", "上海市")
            self.assertEqual(result["status"], "verification_failed")
            self.assertEqual(result["error_code"], "invalid_response")

    async def test_geocoding_requires_unique_precise_address_and_known_city(self):
        precise = {"location": "121.4,31.2", "formatted_address": "上海市示例路1号",
                   "city": "上海市", "level": "门牌号"}
        for items, city, expected in [([precise], "上海市", "matched"),
                                       ([precise, precise], "上海市", "ambiguous"),
                                       ([{**precise, "level": "区县"}], "上海市", "ambiguous"),
                                       ([precise], "", "ambiguous")]:
            geocoded = {"status": "1", "count": str(len(items)), "geocodes": items}
            async def handler(request):
                return httpx.Response(200, json=geocoded if "geocode" in request.url.path else
                                      {"status": "1", "count": "0", "pois": []})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                self.client._client = client
                result = await self.client.verify_place("演示公园", city, "示例路1号")
                self.assertEqual(result["status"], expected)

    async def test_legacy_search_and_geocode_contracts_are_preserved(self):
        self.assertIsInstance(await self.client.search_pois("演示公园", "上海市"), list)
        self.response = {"status": "1", "geocodes": [{"location": "121.4,31.2", "formatted_address": "示例路"}]}
        self.assertEqual(await self.client.geocode("示例路"), {"lat": 31.2, "lng": 121.4, "formatted": "示例路"})
