"""Personal finish times apply to the whole shared outing, with or without return."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.services import itinerary


class ParticipantDeadlineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        self.people = [{"user_id": 1, "name": "甲", "lat": 0, "lng": 0, "mode": "transit"}]
        self.places = [{"id": i, "name": str(i), "lat": 0, "lng": i,
                        "visit_status": "must_visit", "expected_stay_min": 10} for i in (1, 2)]

    async def route(self, origin, dest, mode):
        return {"duration_min": 5, "distance_km": 1, "estimated": False,
                "fallback_reason": None, "steps": [],
                "polyline": [[origin[1], origin[0]], [dest[1], dest[0]]]}

    async def plan(self, objective="balanced"):
        with patch.object(itinerary, "_route", self.route):
            return await itinerary.plan_itinerary(
                self.people, self.places, self.start, self.start + timedelta(hours=3),
                "transit", objective,
            )

    async def test_earliest_personal_deadline_removes_optional_without_return(self):
        self.people[0]["available_until"] = self.start + timedelta(minutes=35)
        self.people.append({**self.people[0], "user_id": 2, "name": "乙",
                            "available_until": self.start + timedelta(hours=2)})
        self.places.append({"id": 3, "name": "可选", "lat": 0, "lng": 3,
                            "visit_status": "optional", "expected_stay_min": 10})
        for objective in ("balanced", "total_time", "distance"):
            with self.subTest(objective=objective):
                result = await self.plan(objective)
                self.assertEqual({s["destination_id"] for s in result["ordered_stops"]}, {1, 2})
                self.assertLessEqual(datetime.fromisoformat(result["finish_at"]), self.people[0]["available_until"])
                self.assertIn("optional_skipped", {w["code"] for w in result["warnings"]})
                self.assertNotIn("participant_leaves_early", {w["code"] for w in result["warnings"]})

    async def test_deadline_during_last_stay_warns_and_preserves_must_visits(self):
        self.people[0]["available_until"] = self.start + timedelta(minutes=25)
        result = await self.plan()
        self.assertEqual(len(result["ordered_stops"]), 2)
        last = result["ordered_stops"][-1]
        self.assertLess(datetime.fromisoformat(last["arrival_at"]), self.people[0]["available_until"])
        warnings = [w for w in result["warnings"] if w["code"] == "participant_leaves_early"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["destination_id"], last["destination_id"])
        self.assertEqual(warnings[0]["user_id"], 1)
        self.assertNotIn("exceeds_end_time", {w["code"] for w in result["warnings"]})

    async def test_finishing_exactly_at_deadline_is_allowed(self):
        self.people[0]["available_until"] = self.start + timedelta(minutes=30)
        result = await self.plan()
        self.assertEqual(datetime.fromisoformat(result["finish_at"]), self.people[0]["available_until"])
        self.assertFalse(result["warnings"])

    async def test_waiting_absorbs_optional_saving_so_do_not_drop_point_needlessly(self):
        self.people[0]["available_until"] = self.start + timedelta(minutes=65)
        self.places[1].update(visit_window_start=self.start + timedelta(minutes=60),
                              visit_window_end=self.start + timedelta(minutes=90))
        self.places.append({"id": 3, "name": "可选", "lat": 0, "lng": 3,
                            "visit_status": "optional", "expected_stay_min": 10})
        result = await self.plan()
        self.assertEqual({s["destination_id"] for s in result["ordered_stops"]}, {1, 2, 3})
        self.assertNotIn("optional_skipped", {w["code"] for w in result["warnings"]})
        self.assertIn("participant_leaves_early", {w["code"] for w in result["warnings"]})
