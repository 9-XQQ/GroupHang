import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.schemas import DestinationCreate, Location, LoginRequest, ParticipantUpdate, VoteRequest
from app.models import TripVote
from app.routers.votes import aggregate_votes
from app.services import itinerary, meeting_point
from app.services.amap import AmapClient
from app.services.access import require_trip_active, require_trip_member
from app.services.itinerary import plan_itinerary
from app.services.llm_parser import LlmPlaceParser
from app.services.shared_text import parse_shared_text


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
    async def test_personal_availability_delays_departure_and_reports_early_end(self):
        start = datetime(2026, 9, 9, 1, 0, tzinfo=timezone.utc)
        participants = [{
            "user_id": 1, "name": "晚到用户", "mode": "transit", "lat": 39.90, "lng": 116.40,
            "available_from": start + timedelta(minutes=30),
            "available_until": start + timedelta(minutes=35),
        }]
        destinations = [
            {"id": 1, "name": "A", "lat": 39.91, "lng": 116.41, "expected_stay_min": 20, "visit_status": "must_visit", "opening_hours": None},
            {"id": 2, "name": "B", "lat": 39.92, "lng": 116.42, "expected_stay_min": 20, "visit_status": "must_visit", "opening_hours": None},
        ]
        detail = {"duration_min": 10.0, "distance_km": 2.0, "polyline": [[116.4, 39.9], [116.41, 39.91]], "steps": []}
        with patch.object(itinerary.amap, "route_detail", AsyncMock(return_value=detail)):
            result = await plan_itinerary(participants, destinations, start, start + timedelta(hours=3), "transit", "total_time")
        arrival = result["participant_arrivals"][0]
        self.assertEqual(arrival["depart_at"], (start + timedelta(minutes=30)).isoformat())
        self.assertEqual(arrival["arrival_at"], (start + timedelta(minutes=40)).isoformat())
        self.assertTrue(any(w["code"] == "participant_cannot_reach_first_stop" for w in result["warnings"]))

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


class AmapPoiSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_search_is_city_limited_and_skips_invalid_locations(self):
        client = AmapClient(key="test-key")
        response = {"pois": [
            {"id": "B001", "name": "岳麓山南门", "address": "登高路", "adname": "岳麓区",
             "cityname": "长沙市", "type": "风景名胜", "location": "112.937100,28.185200"},
            {"id": "BROKEN", "name": "无坐标候选"},
        ]}
        with patch.object(client, "_get", AsyncMock(return_value=response)) as get:
            results = await client.search_pois("岳麓山 南门", "长沙市", 5)

        self.assertEqual(get.await_args.args[0], "/v3/place/text")
        self.assertEqual(get.await_args.args[1]["city"], "长沙市")
        self.assertEqual(get.await_args.args[1]["citylimit"], "true")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["poi_id"], "B001")
        self.assertEqual(results[0]["lat"], 28.1852)


class AmapShareLinkTests(unittest.IsolatedAsyncioTestCase):
    def test_wb_share_p_parameter_extracts_lat_lng_name_and_address(self):
        place = AmapClient._place_from_amap_url(
            "https://wb.amap.com/?p=B0JG77TJ8F%2C23.124446989180413%2C113.24179038405416%2C"
            "%E9%BB%84%E8%AE%B0%E7%B1%B3%E7%B3%95%2C%E8%8D%94%E6%B9%BE%E8%B7%AF3%E5%8F%B7"
        )
        self.assertEqual(place["poi_id"], "B0JG77TJ8F")
        self.assertEqual(place["name"], "黄记米糕")
        self.assertEqual(place["address"], "荔湾路3号")
        self.assertAlmostEqual(place["lat"], 23.124446989180413)
        self.assertAlmostEqual(place["lng"], 113.24179038405416)

    def test_direct_marker_url_extracts_gcj02_coordinate_and_name(self):
        place = AmapClient._place_from_amap_url(
            "https://uri.amap.com/marker?position=112.9371,28.1852&name=%E5%B2%B3%E9%BA%93%E5%B1%B1%E5%8D%97%E9%97%A8"
        )
        self.assertEqual(place["name"], "岳麓山南门")
        self.assertEqual(place["lng"], 112.9371)
        self.assertEqual(place["lat"], 28.1852)

    async def test_non_amap_url_is_not_accepted_for_remote_resolution(self):
        client = AmapClient(key="test-key")
        result = await client.resolve_amap_share_url("https://example.com/?position=112.9,28.1")
        self.assertIsNone(result)
        self.assertIn("不是受支持", client.last_error)

    async def test_direct_amap_url_needs_no_network_request(self):
        client = AmapClient(key="")
        with patch.object(client, "_http_client") as http_client:
            result = await client.resolve_amap_share_url(
                "https://uri.amap.com/marker?position=116.3972,39.9163&name=%E6%95%85%E5%AE%AB"
            )
        http_client.assert_not_called()
        self.assertEqual(result["name"], "故宫")


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

    def test_participant_availability_window_is_validated(self):
        start = datetime(2026, 9, 9, 1, 0, tzinfo=timezone.utc)
        ParticipantUpdate(
            start_location=Location(lat=39.9, lng=116.4), transport_mode="transit",
            available_from=start, available_until=start + timedelta(hours=2),
        )
        with self.assertRaises(ValidationError):
            ParticipantUpdate(
                start_location=Location(lat=39.9, lng=116.4), transport_mode="transit",
                available_from=start, available_until=start,
            )


class SharedTextTests(unittest.TestCase):
    def test_parses_labeled_place_and_source_url(self):
        result = parse_shared_text(
            "名称：故宫博物院\n地址：北京市东城区景山前街4号\n品类：景点\n"
            "人均：60元\n推荐理由：第一次来北京值得去\nhttps://example.com/post"
        )
        self.assertEqual(result["candidates"][0]["name"], "故宫博物院")
        self.assertEqual(result["candidates"][0]["address"], "北京市东城区景山前街4号")
        self.assertEqual(result["candidates"][0]["price"], "60元")
        self.assertEqual(result["source_urls"], ["https://example.com/post"])

    def test_parses_multiple_pipe_separated_lines(self):
        result = parse_shared_text(
            "地点A｜北京市东城区地址A｜餐厅｜100元｜推荐菜A\n"
            "地点B｜北京市西城区地址B｜景点｜免费｜适合拍照",
            default_stay_min=90,
        )
        self.assertEqual([item["name"] for item in result["candidates"]], ["地点A", "地点B"])
        self.assertTrue(all(item["expected_stay_min"] == 90 for item in result["candidates"]))


class LlmPlaceParserTests(unittest.TestCase):
    def test_valid_fenced_json_is_schema_validated(self):
        places = LlmPlaceParser.parse_json_content(
            '```json\n{"places":[{"name":"岳麓山南门","address":"长沙市岳麓区登高路",'
            '"category":"景点","price":"免费","reason":"方便进入"}]}\n```'
        )
        self.assertEqual(places[0]["name"], "岳麓山南门")
        self.assertEqual(places[0]["category"], "景点")

    def test_non_json_or_missing_name_is_rejected(self):
        with self.assertRaises((ValueError, ValidationError)):
            LlmPlaceParser.parse_json_content("不是 JSON")
        with self.assertRaises((ValueError, ValidationError)):
            LlmPlaceParser.parse_json_content('{"places":[{"address":"长沙市"}]}')


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

    async def test_confirmed_trip_rejects_mutation(self):
        db = _FakeDb(trip=SimpleNamespace(status="finished"), participant=None)
        with self.assertRaises(HTTPException) as caught:
            await require_trip_active(db, 1)
        self.assertEqual(caught.exception.status_code, 409)

    async def test_active_trip_allows_mutation(self):
        trip = SimpleNamespace(status="active")
        db = _FakeDb(trip=trip, participant=None)
        self.assertIs(await require_trip_active(db, 1), trip)


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
