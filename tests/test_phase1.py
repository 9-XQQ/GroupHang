import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.schemas import DestinationCreate, Location, LoginRequest, VoteRequest
from app.models import TripVote
from app.routers.votes import aggregate_votes
from app.services import itinerary, meeting_point
from app.services.amap import AmapClient
from app.services.access import require_trip_member
from app.services.itinerary import plan_itinerary


class MeetingPointTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_candidates_awaits_nearby_pois(self):
        participants = [
            {"user_id": 1, "name": "甲", "mode": "transit", "lat": 39.90, "lng": 116.40},
            {"user_id": 2, "name": "乙", "mode": "driving", "lat": 39.91, "lng": 116.41},
        ]
        poi = {
            "name": "测试商圈", "address": "测试地址", "lat": 39.905, "lng": 116.405,
            "category": "商圈", "source": "poi",
        }
        with patch.object(meeting_point.amap, "available", True), patch.object(
            meeting_point.amap, "nearby_pois", AsyncMock(return_value=[poi])
        ) as nearby:
            candidates = await meeting_point.generate_candidates(participants)

        nearby.assert_awaited_once()
        self.assertTrue(any(item["name"] == "测试商圈" for item in candidates))

    async def test_driving_batch_result_maps_to_driver_when_transit_is_first(self):
        participants = [
            {"user_id": 10, "name": "公交用户", "mode": "transit", "lat": 39.90, "lng": 116.40},
            {"user_id": 20, "name": "自驾用户", "mode": "driving", "lat": 39.91, "lng": 116.41},
        ]
        candidate = {
            "name": "候选点", "address": "", "lat": 39.905, "lng": 116.405,
            "category": "商圈", "source": "poi",
        }
        with patch.object(
            meeting_point, "generate_candidates", AsyncMock(return_value=[candidate])
        ), patch.object(
            meeting_point.amap, "driving_durations_batch", AsyncMock(return_value={0: 12.0})
        ), patch.object(
            meeting_point.amap, "transit_duration", AsyncMock(return_value=20.0)
        ), patch.object(meeting_point.amap, "available", False):
            result = await meeting_point.recommend(participants, "minimax")

        by_user = {p["user_id"]: p for p in result[0]["per_person"]}
        self.assertEqual(result[0]["candidate_id"], "coord:39.905000,116.405000")
        self.assertEqual(by_user[20]["travel_time_min"], 21)  # 12 分钟驾车 + 9 分钟停车
        self.assertIn("实时路况", by_user[20]["route_summary"])


class ItineraryTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_participant_can_plan_multiple_destinations(self):
        participants = [
            {"user_id": 1, "name": "独行用户", "mode": "driving", "lat": 39.90, "lng": 116.40},
        ]
        destinations = [
            {"id": 1, "name": "A", "lat": 39.91, "lng": 116.41, "expected_stay_min": 30, "visit_status": "must_visit", "opening_hours": None},
            {"id": 2, "name": "B", "lat": 39.92, "lng": 116.42, "expected_stay_min": 45, "visit_status": "must_visit", "opening_hours": None},
        ]
        detail = {
            "duration_min": 10.0, "distance_km": 4.0,
            "polyline": [[116.40, 39.90], [116.41, 39.91]],
            "steps": [{"type": "driving", "instruction": "沿测试路直行"}],
        }
        start = datetime(2026, 9, 7, 1, 0, tzinfo=timezone.utc)
        with patch.object(itinerary.amap, "route_detail", AsyncMock(return_value=detail)):
            result = await plan_itinerary(participants, destinations, start, start + timedelta(hours=4), "driving", "total_time")
        self.assertEqual(len(result["participant_arrivals"]), 1)
        self.assertEqual(len(result["ordered_stops"]), 2)
        self.assertEqual(result["route_legs"][0]["steps"][0]["instruction"], "沿测试路直行")

    async def test_plan_covers_all_destinations_and_builds_timeline(self):
        participants = [
            {"user_id": 1, "name": "甲", "mode": "driving", "lat": 39.90, "lng": 116.40},
            {"user_id": 2, "name": "乙", "mode": "transit", "lat": 39.95, "lng": 116.35},
        ]
        destinations = [
            {"id": 1, "name": "A", "lat": 39.91, "lng": 116.39, "expected_stay_min": 30, "visit_status": "must_visit", "opening_hours": None},
            {"id": 2, "name": "B", "lat": 39.92, "lng": 116.38, "expected_stay_min": 30, "visit_status": "must_visit", "opening_hours": None},
            {"id": 3, "name": "C", "lat": 39.93, "lng": 116.37, "expected_stay_min": 30, "visit_status": "optional", "opening_hours": None},
        ]
        start = datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc)
        with patch.object(itinerary.amap, "route_detail", AsyncMock(return_value=None)):
            result = await plan_itinerary(participants, destinations, start, start + timedelta(hours=8), "transit", "total_time")
        self.assertEqual({s["destination_id"] for s in result["ordered_stops"]}, {1, 2, 3})
        self.assertEqual(len(result["route_legs"]), 2)
        self.assertEqual(len(result["participant_arrivals"]), 2)
        self.assertTrue(any(w["code"] == "estimated_routes" for w in result["warnings"]))

    async def test_optional_destination_is_skipped_when_time_is_short(self):
        participants = [
            {"user_id": 1, "name": "甲", "mode": "driving", "lat": 39.90, "lng": 116.40},
            {"user_id": 2, "name": "乙", "mode": "driving", "lat": 39.90, "lng": 116.40},
        ]
        destinations = [
            {"id": 1, "name": "必去A", "lat": 39.90, "lng": 116.40, "expected_stay_min": 30, "visit_status": "must_visit", "opening_hours": None},
            {"id": 2, "name": "必去B", "lat": 39.901, "lng": 116.401, "expected_stay_min": 30, "visit_status": "must_visit", "opening_hours": None},
            {"id": 3, "name": "可选C", "lat": 39.902, "lng": 116.402, "expected_stay_min": 120, "visit_status": "optional", "opening_hours": None},
        ]
        start = datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc)
        with patch.object(
            itinerary.amap, "route_detail",
            AsyncMock(return_value={"duration_min": 1.0, "distance_km": 0.5, "polyline": [[116.4, 39.9], [116.401, 39.901]]}),
        ):
            result = await plan_itinerary(participants, destinations, start, start + timedelta(minutes=90), "driving", "total_time")
        self.assertEqual({s["destination_id"] for s in result["ordered_stops"]}, {1, 2})
        self.assertTrue(any(w["code"] == "optional_skipped" for w in result["warnings"]))

    async def test_real_route_geometry_is_kept_in_plan_snapshot(self):
        participants = [
            {"user_id": 1, "name": "甲", "mode": "driving", "lat": 39.90, "lng": 116.40},
            {"user_id": 2, "name": "乙", "mode": "transit", "lat": 39.91, "lng": 116.41},
        ]
        destinations = [
            {"id": 1, "name": "A", "lat": 39.92, "lng": 116.42, "expected_stay_min": 10, "visit_status": "must_visit", "opening_hours": None},
            {"id": 2, "name": "B", "lat": 39.93, "lng": 116.43, "expected_stay_min": 10, "visit_status": "must_visit", "opening_hours": None},
        ]
        detail = {"duration_min": 8.0, "distance_km": 3.2, "polyline": [[116.40, 39.90], [116.415, 39.915], [116.42, 39.92]], "steps": [{"type": "transit", "line": "地铁1号线"}]}
        start = datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc)
        with patch.object(itinerary.amap, "route_detail", AsyncMock(return_value=detail)):
            result = await plan_itinerary(participants, destinations, start, start + timedelta(hours=4), "driving", "distance")
        self.assertEqual(len(result["participant_arrivals"]), 2)
        self.assertEqual(result["participant_arrivals"][0]["polyline"], detail["polyline"])
        self.assertEqual(result["route_legs"][0]["polyline"], detail["polyline"])
        self.assertEqual(result["route_legs"][0]["steps"], detail["steps"])
        self.assertFalse(result["participant_arrivals"][0]["estimated"])


class AmapParsingTests(unittest.TestCase):
    def test_polyline_fragments_are_parsed_and_deduplicated(self):
        points = AmapClient._collect_polylines(["116.1,39.1;116.2,39.2", "116.2,39.2;116.3,39.3"])
        self.assertEqual(points, [[116.1, 39.1], [116.2, 39.2], [116.3, 39.3]])

    def test_decimal_duration_string_is_supported(self):
        self.assertEqual(AmapClient._duration_to_minutes("1551.0"), 25.9)


class AmapTransitTests(unittest.IsolatedAsyncioTestCase):
    async def test_transit_uses_integrated_endpoint_and_returns_steps(self):
        client = AmapClient(key="test-key")
        response = {
            "route": {"transits": [{
                "duration": "1800", "distance": "10000",
                "segments": [{"walking": {"steps": []}, "bus": {"buslines": [{
                    "name": "地铁1号线", "polyline": "116.1,39.1;116.2,39.2",
                    "departure_stop": {"name": "甲站"}, "arrival_stop": {"name": "乙站"},
                    "via_num": "3", "duration": "1200", "distance": "8000",
                }]}}],
            }]},
        }
        with patch.object(client, "_resolve_city", AsyncMock(return_value="010")), patch.object(
            client, "_get", AsyncMock(return_value=response)
        ) as get:
            result = await client.route_detail((39.1, 116.1), (39.2, 116.2), "transit")
        self.assertEqual(get.await_args.args[0], "/v3/direction/transit/integrated")
        self.assertEqual(result["duration_min"], 30.0)
        self.assertEqual(result["steps"][0]["line"], "地铁1号线")


class SchemaTests(unittest.TestCase):
    def test_phone_must_be_mainland_mobile_number(self):
        LoginRequest(phone="13800000000")
        with self.assertRaises(ValidationError):
            LoginRequest(phone="12345")

    def test_nickname_is_optional(self):
        request = LoginRequest(phone="13800000000")
        self.assertIsNone(request.name)

    def test_vote_value_is_limited(self):
        VoteRequest(candidate_id="candidate-1", vote_value=1)
        with self.assertRaises(ValidationError):
            VoteRequest(candidate_id="candidate-1", vote_value=2)

    def test_destination_fields_are_validated(self):
        destination = DestinationCreate(name="故宫", lat=39.9163, lng=116.3972)
        self.assertEqual(destination.expected_stay_min, 60)
        with self.assertRaises(ValidationError):
            DestinationCreate(name="故宫", lat=39.9163, lng=116.3972, expected_stay_min=2)

    def test_destination_vote_type_is_supported(self):
        vote = VoteRequest(candidate_type="destination", candidate_id="21", vote_value=1)
        self.assertEqual(vote.candidate_type, "destination")

    def test_location_range_is_checked(self):
        with self.assertRaises(ValidationError):
            Location(lat=100, lng=116.4)


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeDb:
    def __init__(self, trip, participant):
        self.trip = trip
        self.participant = participant

    async def get(self, model, key):
        return self.trip

    async def execute(self, statement):
        return _ScalarResult(self.participant)


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_member_is_rejected(self):
        db = _FakeDb(trip=object(), participant=None)
        with self.assertRaises(HTTPException) as caught:
            await require_trip_member(db, 1, SimpleNamespace(id=99))
        self.assertEqual(caught.exception.status_code, 403)

    async def test_member_is_allowed(self):
        participant = SimpleNamespace(id=7)
        db = _FakeDb(trip=object(), participant=participant)
        self.assertIs(await require_trip_member(db, 1, SimpleNamespace(id=1)), participant)


class VoteAggregationTests(unittest.TestCase):
    def test_counts_and_current_users_vote(self):
        rows = [
            TripVote(candidate_type="meeting_point", candidate_id="coord:1", user_id=1, vote_value=1),
            TripVote(candidate_type="meeting_point", candidate_id="coord:1", user_id=2, vote_value=-1),
        ]
        result = aggregate_votes(rows, current_user_id=1)
        self.assertEqual(
            result,
            [{
                "candidate_type": "meeting_point", "candidate_id": "coord:1",
                "up": 1, "down": 1, "my_vote": 1,
            }],
        )


if __name__ == "__main__":
    unittest.main()
