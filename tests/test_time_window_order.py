"""Bounded rescheduling reduces avoidable fixed-window conflicts without new map calls."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.services import itinerary


class TimeWindowOrderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        self.places = [
            {"id": 1, "name": "集合", "lat": 0, "lng": 1, "visit_status": "must_visit", "expected_stay_min": 1},
            {"id": 2, "name": "普通地点", "lat": 0, "lng": 2, "visit_status": "must_visit", "expected_stay_min": 1},
            {"id": 3, "name": "预约", "lat": 0, "lng": 3, "visit_status": "must_visit", "expected_stay_min": 10,
             "visit_window_start": self.start, "visit_window_end": self.start + timedelta(minutes=13)},
        ]
        self.people = [{"user_id": 1, "name": "甲", "lat": 0, "lng": 0, "mode": "transit"}]
        self.calls = 0

    async def route(self, origin, dest, mode):
        self.calls += 1
        # 赴约非集合点慢；普通点到预约点有单向绕行，预约后再去普通点可行。
        duration = 30 if origin == (0, 0) and dest != (0, 1) else 10 if (origin, dest) == ((0, 2), (0, 3)) else 1
        return {"duration_min": duration, "distance_km": duration, "estimated": False,
                "fallback_reason": None, "steps": [],
                "polyline": [[origin[1], origin[0]], [dest[1], dest[0]]]}

    async def test_reordering_recovers_feasible_appointment_for_all_objectives(self):
        for objective in ("balanced", "total_time", "distance"):
            with self.subTest(objective=objective), patch.object(itinerary, "_route", self.route):
                self.calls = 0
                result = await itinerary.plan_itinerary(
                    self.people, self.places, self.start, self.start + timedelta(hours=3), "transit", objective,
                )
                self.assertEqual([s["destination_id"] for s in result["ordered_stops"]], [1, 3, 2])
                appointment = result["ordered_stops"][1]
                self.assertLessEqual(datetime.fromisoformat(appointment["leave_at"]), self.places[2]["visit_window_end"])
                self.assertNotIn("destination_time_window_conflict", {w["code"] for w in result["warnings"]})
                self.assertEqual(self.calls, 9)  # 6 directed inter-stop routes + 3 personal arrivals.

    def test_return_and_deadline_are_part_of_reordering(self):
        places = [{**p, "expected_stay_min": 1} for p in self.places]
        places[2]["visit_window_end"] = self.start + timedelta(hours=3)
        matrix = [[0 if i == j else 1 for j in range(3)] for i in range(3)]
        returns = [{"duration_min": x, "distance_km": x} for x in (0, 0, 100)]
        order = itinerary._improve_time_order(
            [0, 1, 2], places, self.start, matrix, matrix,
            self.start + timedelta(minutes=10), returns, "distance",
        )
        self.assertEqual(order, [0, 2, 1])

    async def test_impossible_must_visit_keeps_place_and_conflict(self):
        self.places[2]["visit_window_end"] = self.start + timedelta(minutes=5)
        with patch.object(itinerary, "_route", self.route):
            result = await itinerary.plan_itinerary(
                self.people, self.places, self.start, self.start + timedelta(hours=3), "transit", "balanced",
            )
        self.assertEqual({s["destination_id"] for s in result["ordered_stops"]}, {1, 2, 3})
        self.assertIn("destination_time_window_conflict", {w["code"] for w in result["warnings"]})

    def test_ten_stop_search_is_bounded_deterministic_and_preserves_first(self):
        places = [{"id": i, "expected_stay_min": 1} for i in range(10)]
        places[-1].update(visit_window_start=self.start, visit_window_end=self.start + timedelta(minutes=20))
        matrix = [[0 if i == j else (i * 7 + j * 3) % 11 + 1 for j in range(10)] for i in range(10)]
        args = (list(range(10)), places, self.start, matrix, matrix,
                self.start + timedelta(hours=3), [], "total_time")
        baseline = itinerary._schedule_order(args[0], places, self.start, matrix)
        with patch.object(itinerary, "_schedule_order", wraps=itinerary._schedule_order) as schedule:
            order = itinerary._improve_time_order(*args)
            self.assertLessEqual(schedule.call_count, 1 + 10 * 9 * 9)
        improved = itinerary._schedule_order(order, places, self.start, matrix)
        self.assertLessEqual(improved["window_penalty"], baseline["window_penalty"])
        self.assertEqual(order[0], 0)
        self.assertEqual(set(order), set(range(10)))
        self.assertEqual(order, itinerary._improve_time_order(*args))
