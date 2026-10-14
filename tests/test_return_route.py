"""Shared return targets contribute to time budgets and immutable plan snapshots."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.services import itinerary


class ReturnRouteTests(unittest.IsolatedAsyncioTestCase):
    def inputs(self, optional=False):
        people = [{"user_id":1, "name":"A", "lat":0, "lng":0, "mode":"transit"}]
        places = [{"id":i, "name":str(i), "lat":0, "lng":i, "visit_status":"must_visit",
                   "expected_stay_min":10} for i in (1,2)]
        if optional:
            places.append({"id":3,"name":"optional","lat":0,"lng":3,
                           "visit_status":"optional","expected_stay_min":10})
        return people, places

    async def route(self, origin, dest, mode):
        return {"duration_min":15 if dest == (1,1) else 5, "distance_km":1,
                "estimated":False, "fallback_reason":None, "steps":[],
                "polyline":[[origin[1],origin[0]],[dest[1],dest[0]]]}

    async def test_return_is_included_in_elapsed_travel_and_distance_totals(self):
        people, places = self.inputs()
        start = datetime(2026,11,1,tzinfo=timezone.utc)
        with patch.object(itinerary,"_route",self.route):
            original = await itinerary.plan_itinerary(people,places,start,start+timedelta(hours=3),"transit","balanced")
            returned = await itinerary.plan_itinerary(people,places,start,start+timedelta(hours=3),"transit","balanced",
                                                      return_location={"lat":1,"lng":1,"address":"中心"})
        self.assertEqual(returned["total_duration_min"],original["total_duration_min"]+15)
        self.assertEqual(returned["total_travel_min"],original["total_travel_min"]+15)
        self.assertEqual(returned["total_distance_km"],original["total_distance_km"]+1)
        self.assertEqual(returned["route_legs"][-1]["kind"],"return")
        self.assertEqual(returned["finish_at"],returned["return_leg"]["arrival_at"])
        self.assertEqual(returned["return_leg"]["return_location"]["address"],"中心")

    async def test_optional_skip_uses_return_budget_but_keeps_must_visit(self):
        people,places=self.inputs(optional=True)
        start=datetime(2026,11,1,tzinfo=timezone.utc)
        with patch.object(itinerary,"_route",self.route):
            result=await itinerary.plan_itinerary(people,places,start,start+timedelta(minutes=59),"transit","balanced",
                                                 return_location={"lat":1,"lng":1})
        ids={p["destination_id"] for p in result["ordered_stops"]}
        self.assertEqual(ids,{1,2})
        self.assertTrue(any(w["code"]=="optional_skipped" for w in result["warnings"]))
        self.assertLessEqual(datetime.fromisoformat(result["finish_at"]),start+timedelta(minutes=59))

    async def test_estimated_return_and_personal_deadline_are_explicit(self):
        people,places=self.inputs()
        start=datetime(2026,11,1,tzinfo=timezone.utc)
        people[0]["available_until"]=start+timedelta(minutes=35)
        async def estimated(origin,dest,mode):
            result=await self.route(origin,dest,mode)
            if dest==(1,1):
                result.update(estimated=True,fallback_reason="offline")
            return result
        with patch.object(itinerary,"_route",estimated):
            result=await itinerary.plan_itinerary(people,places,start,start+timedelta(minutes=35),"transit","balanced",
                                                 return_location={"lat":1,"lng":1})
        codes={w["code"] for w in result["warnings"]}
        self.assertTrue({"estimated_routes","return_after_participant_deadline","exceeds_end_time"} <= codes)
        self.assertTrue(result["return_leg"]["estimated"])

    async def test_last_stop_already_at_return_target_has_zero_cost(self):
        people,places=self.inputs()
        start=datetime(2026,11,1,tzinfo=timezone.utc)
        with patch.object(itinerary,"_route",self.route):
            result=await itinerary.plan_itinerary(people,places,start,start+timedelta(hours=3),"transit","balanced",
                                                 return_location={"lat":0,"lng":2})
        self.assertEqual(result["ordered_stops"][-1]["destination_id"],2)
        self.assertEqual(result["return_leg"]["travel_time_min"],0)
        self.assertFalse(result["return_leg"]["estimated"])

    async def test_agent_comparison_includes_return_in_all_objectives(self):
        from app.routers import agent
        from app.services.planning_input import PlanningInput
        people, places = self.inputs()
        start = datetime(2026,11,1,tzinfo=timezone.utc)
        inputs = PlanningInput("itinerary",start,start+timedelta(hours=3),"transit","balanced",
                               people,places,{"lat":1,"lng":1})
        with patch.object(itinerary,"_route",self.route):
            result = await agent._compare_inputs(inputs,1)
        self.assertEqual(len(result["comparisons"]),3)
        self.assertTrue(all(item["has_return_leg"] for item in result["comparisons"]))
        self.assertTrue(all(item["total_duration_min"] >= 45 for item in result["comparisons"]))
