"""Saved-plan diffs are deterministic, private to members, and require no map calls."""
import os
import unittest
from copy import deepcopy
from unittest.mock import patch, AsyncMock

from app.models import ItineraryPlan, Trip
from app.services.plan_review import compare_plans
from tests import test_password_auth as password_tests


def snapshot(plan_id=1):
    return {"plan_id":plan_id,"input_version":1,"total_travel_min":20,"total_duration_min":60,
            "warnings":[],"route_legs":[{"distance_km":2}],
            "ordered_stops":[{"destination_id":i,"name":str(i),"order_index":i,
                              "arrival_at":"2026-11-01T13:00:00+08:00", "leave_at":"2026-11-01T13:20:00+08:00"}
                             for i in (1,2)]}


class PlanReviewTests(unittest.TestCase):
    def test_added_removed_reordered_timing_metrics_and_return_changes(self):
        previous=snapshot()
        current=deepcopy(previous)
        current.update(plan_id=2,input_version=2,total_travel_min=15,total_duration_min=70)
        current["ordered_stops"]=[{**current["ordered_stops"][1],"order_index":1,
                                   "leave_at":"2026-11-01T13:30:00+08:00"},
                                  {**current["ordered_stops"][0],"destination_id":3,"name":"3","order_index":2}]
        current["route_legs"]=[{"kind":"return","distance_km":4,"return_location":{"lat":1,"lng":2}}]
        diff=compare_plans(previous,current)
        self.assertEqual(diff["added"],[{"id":3,"name":"3"}])
        self.assertEqual(diff["removed"],[{"id":1,"name":"1"}])
        self.assertEqual(diff["moved"],[{"id":2,"name":"2","before":2,"after":1}])
        self.assertEqual(len(diff["timing_changed"]),1)
        self.assertEqual(diff["travel_delta_min"],-5)
        self.assertEqual(diff["duration_delta_min"],10)
        self.assertEqual(diff["distance_delta_km"],2)
        self.assertTrue(diff["return_changed"])
        self.assertFalse(diff["same_inputs"])
        self.assertEqual(previous,snapshot(),"Diff must not mutate snapshots")

    def test_unchanged_snapshots_have_no_fabricated_changes(self):
        previous=snapshot()
        current={**previous,"plan_id":2}
        diff=compare_plans(previous,current)
        for key in ("added","removed","moved","timing_changed"):
            self.assertEqual(diff[key],[])
        self.assertEqual(diff["duration_delta_min"],0)
        self.assertFalse(diff["return_changed"])
        self.assertTrue(diff["same_inputs"])


@unittest.skipUnless(os.getenv("AGENT_DB_TESTS")=="1","set AGENT_DB_TESTS=1")
class PlanReviewDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=password_tests.PasswordAuthDatabaseTests.asyncSetUp
    cleanup_schema=password_tests.PasswordAuthDatabaseTests.cleanup_schema
    register=password_tests.PasswordAuthDatabaseTests.register

    async def test_member_only_latest_two_snapshots_and_stale_status(self):
        registered=await self.register()
        headers={"X-Token":registered.json()["token"]}
        trip=await self.client.post("/trips",json={"title":"compare"},headers=headers)
        trip_id=trip.json()["trip_id"]
        url=f"/trips/{trip_id}/itinerary-plans/comparison"
        self.assertFalse((await self.client.get(url,headers=headers)).json()["available"])
        async with self.sessions() as db:
            row=await db.get(Trip,trip_id)
            row.input_version=2
            for version in (1,2):
                data=snapshot(version)
                db.add(ItineraryPlan(trip_id=trip_id,input_version=version,objective="balanced",
                    first_destination_id=None,participant_arrivals=[],ordered_stops=data["ordered_stops"],
                    route_legs=data["route_legs"],warnings=[],total_travel_min=20-version,total_duration_min=60))
            await db.commit()
        with patch("app.services.itinerary.amap.route_detail",AsyncMock(side_effect=AssertionError("No map calls"))):
            result=await self.client.get(url,headers=headers)
        self.assertEqual(result.status_code,200)
        self.assertTrue(result.json()["available"])
        self.assertTrue(result.json()["previous_is_stale"])
        self.assertFalse(result.json()["current_is_stale"])
        self.assertEqual(result.json()["changes"]["travel_delta_min"],-1)
        self.assertEqual((await self.client.get(url)).status_code,401)
        outsider=await self.register("outsider")
        self.assertEqual((await self.client.get(url,headers={"X-Token":outsider.json()["token"]})).status_code,403)
