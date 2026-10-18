"""B1 route metadata with all collaborators mocked; never connects to a DB."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.routers import shared_text as route
from app.schemas import PlaceVerification, SharedTextParseRequest


class PlacePreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_states_are_exposed_without_persistence(self):
        place = {"name": "演示公园", "address": "示例路1号", "lat": 31.2, "lng": 121.4, "poi_id": "synthetic"}
        for status in ("matched", "ambiguous", "not_found", "verification_failed", "unverified"):
            verification = PlaceVerification(status=status, source="poi", complete=status == "matched",
                selected=place if status == "matched" else None,
                candidates=[place] if status in ("matched", "ambiguous") else [],
                error_code="timeout" if status == "verification_failed" else None).model_dump()
            with patch.object(route, "require_trip_member", AsyncMock()), patch.object(
                    route.amap, "verify_place", AsyncMock(return_value=verification)):
                result = await route.parse_text(1, SharedTextParseRequest(text="演示公园", preferred_city="上海市"),
                                                SimpleNamespace(id=1), SimpleNamespace())
            candidate = result["candidates"][0]
            self.assertEqual(candidate["verification_status"], status)
            self.assertEqual(candidate["resolved"], status == "matched")
            self.assertEqual(candidate["lat"], 31.2 if status == "matched" else None)
            self.assertEqual(candidate["verification_error_code"], verification["error_code"])
            self.assertIn("parse_candidate_id", candidate)

    async def test_share_coordinates_are_not_claimed_as_independently_verified(self):
        for place in ({"name": "演示", "address": "", "lat": 31.2, "lng": 121.4}, None):
            with patch.object(route, "require_trip_member", AsyncMock()), patch.object(
                    route.amap, "resolve_amap_share_url", AsyncMock(return_value=place)), patch.object(
                    route.amap, "verify_place", AsyncMock()):
                result = await route.parse_text(1, SharedTextParseRequest(text="https://uri.amap.com/marker?position=121.4,31.2"),
                                                SimpleNamespace(id=1), SimpleNamespace())
            self.assertEqual(result["candidates"][0]["verification_status"], "unverified" if place else "verification_failed")
