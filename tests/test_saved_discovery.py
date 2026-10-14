"""Saved library, shared comments and all three Agent tools over isolated HTTP."""
import unittest
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import select, func

from app.db import get_db
from app.main import app
from app.models import SavedPlace, DestinationComment, TripDestination, Trip, TripParticipant, User
from app.security import issue_token
from app.services import place_discovery as tools
from app.schemas import DiscoveryRequest
from tests import test_agent_execution as fixtures


class DiscoveryIntentTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_intents_and_explicit_time(self):
        self.assertEqual(tools.scene_from_message("大家去哪吃饭"), "meal")
        self.assertEqual(tools.scene_from_message("寿司郎分店选哪一家"), "brand")
        self.assertEqual(tools.scene_from_message("在公园附近游玩一下午"), "afternoon")
        self.assertIsNone(tools.scene_from_message("添加餐厅地点"))
        self.assertIsNone(tools.scene_from_message("修改下午的行程时间"))
        result = await tools.interpret(DiscoveryRequest(message="在人民公园附近玩一下午，2026-11-01 13:00到17:00"))
        self.assertEqual(result.scenario, "afternoon")
        self.assertEqual(result.anchor_name, "人民公园")
        self.assertIsNotNone(result.start_at.tzinfo)
        self.assertEqual(result.start_at.hour, 13)

    async def test_llm_extractor_cannot_invent_brand_or_anchor(self):
        with patch.object(tools.llm_place_parser, "complete_json", AsyncMock(return_value=(
                {"scenario": "brand", "brand": "不存在品牌", "anchor_name": "不存在锚点"}, None))):
            request = await tools.interpret(DiscoveryRequest(message="帮我推荐一下"))
        self.assertEqual(request.brand, "")
        self.assertEqual(request.anchor_name, "")


@unittest.skipUnless(os.getenv("AGENT_DB_TESTS") == "1", "set AGENT_DB_TESTS=1")
class SavedDiscoveryDatabaseTests(unittest.IsolatedAsyncioTestCase):
    cleanup_schema = fixtures.AgentExecutionDatabaseTests.cleanup_schema

    async def asyncSetUp(self):
        await fixtures.AgentExecutionDatabaseTests.asyncSetUp(self)
        previous = dict(app.dependency_overrides)
        async def isolated_db():
            async with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = isolated_db
        def restore():
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)
        self.addCleanup(restore)
        async with self.sessions() as db:
            first, _ = await issue_token(db, self.user.id)
            second, _ = await issue_token(db, self.other.id)
            outside = User(name="outsider")
            db.add(outside)
            await db.flush()
            third, _ = await issue_token(db, outside.id)
            await db.commit()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                       headers={"X-Token": first, "X-Requested-With": "TripPlanner"})
        self.other_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                              headers={"X-Token": second})
        self.outside_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                                headers={"X-Token": third})
        for client in (self.client, self.other_client, self.outside_client):
            self.addAsyncCleanup(client.aclose)
        self.base = f"/trips/{self.trip.id}"
        self.route_patch = patch.object(tools, "_route", self.fake_route)
        self.route_patch.start()
        self.addCleanup(self.route_patch.stop)
        self.poi_patch = patch.object(tools.amap, "nearby_pois", self.fake_nearby)
        self.poi_patch.start()
        self.addCleanup(self.poi_patch.stop)
        self.search_patch = patch.object(tools.amap, "search_pois", self.fake_search)
        self.search_patch.start()
        self.addCleanup(self.search_patch.stop)
        self.available_patch = patch.object(tools.amap, "available", True)
        self.available_patch.start()
        self.addCleanup(self.available_patch.stop)

    async def fake_route(self, origin, dest, mode):
        return {"duration_min": round(abs(dest[1] - origin[1]) * 100 + 5, 2), "distance_km": 1,
                "estimated": False, "fallback_reason": None, "steps": [],
                "polyline": [[origin[1], origin[0]], [dest[1], dest[0]]]}

    async def fake_nearby(self, location, **kwargs):
        if kwargs.get("types") == "050000":
            return [{"name": "餐厅一", "lat":31.22, "lng":121.42, "category":"餐饮", "poi_id":"food1"},
                    {"name": "餐厅二", "lat":31.24, "lng":121.44, "category":"餐饮", "poi_id":"food2"}]
        if kwargs.get("types") == "110000|060100":
            return [{"name":"江边公园", "lat":31.22, "lng":121.42, "category":"公园"}]
        return [{"name": "公园一", "lat":31.22, "lng":121.42, "category":"公园", "poi_id":"park1"},
                {"name": "博物馆", "lat":31.23, "lng":121.43, "category":"博物馆", "poi_id":"museum1"},
                {"name": "公园二", "lat":31.24, "lng":121.44, "category":"公园", "poi_id":"park2"}]

    async def fake_search(self, keyword, city, limit=10):
        return [{"name": keyword + "商场店", "lat":31.22, "lng":121.42, "category":"餐饮", "poi_id":"brand1",
                 "city":city, "address":"购物中心"},
                {"name": keyword + "公园店", "lat":31.24, "lng":121.44, "category":"餐饮", "poi_id":"brand2",
                 "city":city, "address":"公园路"}]

    async def save(self, **changes):
        body = {"name":"私人公园", "city":"上海市", "lat":31.22, "lng":121.42,
                "tags":["公园","想去"], "private_note":"PRIVATE_MEMORY_123", "expected_stay_min":30,
                "category":"公园", **changes}
        response = await self.client.post("/users/me/saved-places", json=body)
        self.assertIn(response.status_code, (200, 201), response.text)
        return response.json()

    async def test_private_crud_tags_revision_and_duplicate_save_preserve_memory(self):
        place = await self.save()
        duplicate = await self.save(private_note="overwrite attempt")
        self.assertEqual(place["id"], duplicate["id"])
        self.assertEqual(duplicate["private_note"], "PRIVATE_MEMORY_123")
        listed = await self.client.get("/users/me/saved-places?tag=公园")
        self.assertEqual(listed.json()["total"], 1)
        self.assertEqual(listed.headers["cache-control"], "no-store")
        self.assertEqual((await self.other_client.get("/users/me/saved-places")).json()["total"], 0)
        change = {"revision":1, "tags":["周末"], "private_note":"new memory", "expected_stay_min":45}
        self.assertEqual((await self.other_client.put("/users/me/saved-places/" + str(place["id"]), json=change)).status_code,404)
        updated = await self.client.put("/users/me/saved-places/" + str(place["id"]), json=change)
        self.assertEqual(updated.json()["revision"], 2)
        self.assertEqual((await self.client.put("/users/me/saved-places/" + str(place["id"]), json=change)).status_code,409)
        self.assertEqual((await self.client.get("/users/me/saved-places?tag=公园")).json()["total"], 0)

    async def test_import_is_atomic_private_note_free_versioned_and_idempotent(self):
        place = await self.save()
        body = {"expected_version":1, "places":[{"saved_place_id":place["id"],"revision":1}]}
        first = await self.client.post(self.base + "/places/import", json=body)
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json()["input_version"],2)
        second = await self.client.post(self.base + "/places/import", json=body)
        self.assertTrue(second.json()["replayed"])
        destination_id = first.json()["destination_ids"][0]
        await self.client.delete("/users/me/saved-places/" + str(place["id"]))
        async with self.sessions() as db:
            destination = await db.get(TripDestination, destination_id)
            self.assertEqual(destination.name,"私人公园")
            self.assertIsNone(destination.note)
            self.assertEqual(await db.scalar(select(func.count()).select_from(TripDestination)),3)
        response = await self.client.get(self.base + "/destinations")
        self.assertNotIn("PRIVATE_MEMORY_123",response.text)
        self.assertEqual((await self.outside_client.post(self.base + "/places/import",json=body)).status_code,403)

    async def test_import_rejects_changed_saved_place_and_rolls_back_entire_batch(self):
        place = await self.save()
        body = {"expected_version":1,"places":[
            {"place":{"name":"new first","lat":31.22,"lng":121.42}},
            {"saved_place_id":place["id"],"revision":999}]}
        response=await self.client.post(self.base+"/places/import",json=body)
        self.assertEqual(response.status_code,409)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(TripDestination)),2)
            self.assertEqual((await db.get(Trip,self.trip.id)).input_version,1)

    async def test_comments_shared_to_members_only_and_only_author_can_edit(self):
        path=self.base+f"/destinations/{self.destinations[0].id}/comments"
        comment=await self.client.post(path,json={"content":"同行可见的评论"})
        self.assertEqual(comment.status_code,201,comment.text)
        cid=comment.json()["id"]
        self.assertEqual((await self.other_client.get(path)).json()["comments"][0]["content"],"同行可见的评论")
        self.assertEqual((await self.outside_client.get(path)).status_code,403)
        self.assertEqual((await self.other_client.put(path+f"/{cid}",json={"content":"overwrite"})).status_code,404)
        self.assertEqual((await self.client.put(path+f"/{cid}",json={"content":"updated"})).status_code,200)
        self.assertEqual((await self.client.delete(path+f"/{cid}")).status_code,200)
        self.assertEqual((await self.client.get(path)).json()["comments"],[])
        async with self.sessions() as db:
            self.assertEqual((await db.get(Trip,self.trip.id)).input_version,1)

    async def test_meal_agent_chat_routes_uses_saved_and_online_and_no_private_notes(self):
        await self.save(name="收藏餐厅",category="餐饮")
        response=await self.client.post(self.base+"/assistant/chat",json={
            "message":"大家约着吃饭不知道去哪，推荐餐厅","discovery":{"scenario":"meal","city":"上海市"}})
        self.assertEqual(response.status_code,200,response.text)
        result=response.json()["discovery"]
        self.assertEqual(result["scenario"],"meal")
        self.assertGreater(len(result["recommendations"]),1)
        self.assertNotIn("PRIVATE_MEMORY_123",response.text)
        self.assertEqual(len(result["recommendations"][0]["per_person"]),2)
        self.assertEqual((await self.client.get(self.base+"/destinations")).json()["input_version"],1)

    async def test_brand_ranking_has_fairness_nearby_and_opt_in_preferences(self):
        async with self.sessions() as db:
            member=await db.get(User,self.other.id)
            member.preferences={"explicit":{"preferred_categories":["公园","清净"]}}
            await db.commit()
        body={"scenario":"brand","city":"上海市","brand":"寿司郎","radius_km":10}
        first=await self.client.post(self.base+"/assistant/discover",json=body)
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(len(first.json()["recommendations"]),2)
        self.assertTrue(all(p["preference_matches"] == 0 for p in first.json()["recommendations"]))
        self.assertFalse(any("isolated member" in reason for p in first.json()["recommendations"] for reason in p["reasons"]))
        async with self.sessions() as db:
            participant=await db.scalar(select(TripParticipant).where(
                TripParticipant.trip_id==self.trip.id,TripParticipant.user_id==self.other.id))
            participant.share_preferences=True
            await db.commit()
        shared=await self.client.post(self.base+"/assistant/discover",json=body)
        self.assertIn("本次启用的偏好",shared.text)
        self.assertIn("没有可靠",shared.text)
        times=[p["max_travel_min"] for p in shared.json()["recommendations"]]
        self.assertEqual(times,sorted(times))

    async def test_afternoon_respects_budget_radius_return_and_imports_selected(self):
        start=self.trip.planned_start_at
        body={"scenario":"afternoon","city":"上海市","anchor":{"lat":31.22,"lng":121.42},
              "start_at":start.isoformat(),"end_at":(start+timedelta(hours=3)).isoformat(),
              "radius_km":2,"return_to_anchor":True,"stay_min":45}
        response=await self.client.post(self.base+"/assistant/chat",json={"message":"附近玩一下午","discovery":body})
        self.assertEqual(response.status_code,200,response.text)
        result=response.json()["discovery"]
        self.assertGreater(len(result["recommendations"]),0)
        self.assertLessEqual(datetime.fromisoformat(result["finish_at"]),start+timedelta(hours=3))
        self.assertIsNotNone(result["return_leg"])
        selected=result["recommendations"][0]["selection"]
        imported=await self.client.post(self.base+"/places/import",json={
            "expected_version":result["input_version"],"places":[selected]})
        self.assertEqual(imported.status_code,200,imported.text)
        self.assertEqual(imported.json()["input_version"],2)

    async def test_missing_anchor_time_no_feasible_and_estimated_are_explicit(self):
        async with self.sessions() as db:
            trip=await db.get(Trip,self.trip.id)
            trip.planned_start_at=trip.planned_end_at=None
            await db.commit()
        missing=await self.client.post(self.base+"/assistant/discover",json={"scenario":"afternoon"})
        self.assertTrue(missing.json()["needs"])
        body={"scenario":"afternoon","anchor":{"lat":31.22,"lng":121.42},"saved_only":True,
              "start_at":"2026-11-01T02:00:00Z","end_at":"2026-11-01T02:01:00Z"}
        await self.save()
        impossible=await self.client.post(self.base+"/assistant/discover",json=body)
        self.assertEqual(impossible.json()["recommendations"],[])
        body["end_at"]="2026-11-01T06:00:00Z"
        async def estimate(*args):
            return {"duration_min":5,"distance_km":1,"estimated":True,"fallback_reason":"offline","polyline":[],"steps":[]}
        with patch.object(tools,"_route",estimate):
            estimated=await self.client.post(self.base+"/assistant/discover",json=body)
        self.assertEqual(estimated.json()["feasibility"],"estimated")
        self.assertTrue(estimated.json()["recommendations"][0]["estimated"])

    async def test_agent_recommendation_requires_membership_and_stale_import_is_rejected(self):
        response=await self.outside_client.post(self.base+"/assistant/discover",json={"scenario":"meal"})
        self.assertEqual(response.status_code,403)
        place=await self.save()
        response=await self.client.post(self.base+"/places/import",json={
            "expected_version":999,"places":[{"saved_place_id":place["id"],"revision":1}]})
        self.assertEqual(response.status_code,409)

    async def test_concurrent_import_creates_one_destination_and_preserves_saved_place(self):
        import asyncio
        place=await self.save()
        body={"expected_version":1,"places":[{"saved_place_id":place["id"],"revision":1}]}
        responses=await asyncio.gather(*[self.client.post(self.base+"/places/import",json=body) for _ in range(2)])
        self.assertEqual([r.status_code for r in responses],[200,200])
        self.assertEqual(sum(bool(r.json()["replayed"]) for r in responses),1)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(TripDestination)),3)
            self.assertEqual((await db.get(SavedPlace,place["id"])).private_note,"PRIVATE_MEMORY_123")
            self.assertEqual((await db.get(Trip,self.trip.id)).input_version,2)

    async def test_foreign_saved_place_cannot_be_imported_and_imports_freeze_on_confirm(self):
        place=await self.save()
        body={"expected_version":1,"places":[{"saved_place_id":place["id"],"revision":1}]}
        self.assertEqual((await self.other_client.post(self.base+"/places/import",json=body)).status_code,404)
        async with self.sessions() as db:
            trip=await db.get(Trip,self.trip.id)
            trip.status="confirmed"
            await db.commit()
        self.assertEqual((await self.client.post(self.base+"/places/import",json=body)).status_code,409)

    async def test_afternoon_saved_tags_filters_and_ambiguous_anchor_prompts(self):
        await self.save(tags=["回忆"])
        body={"scenario":"afternoon","saved_only":True,"tags":["回忆"],
              "anchor":{"lat":31.22,"lng":121.42},"radius_km":1}
        response=await self.client.post(self.base+"/assistant/discover",json=body)
        self.assertEqual(len(response.json()["recommendations"]),1,response.text)
        self.assertNotIn("PRIVATE_MEMORY_123",response.text)
        body["tags"]=["不存在"]
        empty=await self.client.post(self.base+"/assistant/discover",json=body)
        self.assertEqual(empty.json()["recommendations"],[])
        ambiguous=await self.client.post(self.base+"/assistant/discover",json={
            "scenario":"afternoon","city":"上海市","anchor_name":"公园"})
        self.assertTrue(ambiguous.json()["needs"])
        self.assertEqual(len(ambiguous.json()["anchor_choices"]),2)

    async def test_missing_participant_location_is_not_guessed(self):
        async with self.sessions() as db:
            participant=await db.scalar(select(TripParticipant).where(
                TripParticipant.trip_id==self.trip.id,TripParticipant.user_id==self.other.id))
            participant.start_location=None
            await db.commit()
        result=await self.client.post(self.base+"/assistant/discover",json={"scenario":"meal"})
        self.assertTrue(result.json()["needs"])
        self.assertEqual(result.json()["recommendations"],[])


    async def handoff_body(self):
        place = await self.save()
        return {"expected_version": 1, "places": [{"saved_place_id": place["id"], "revision": 1}],
                "confirm_for_planning": True, "planning_settings": {
                    "planned_start_at": "2026-11-01T03:00:00Z", "planned_end_at": "2026-11-01T07:00:00Z",
                    "group_transport_mode": "transit", "optimization_objective": "balanced"}}

    async def test_planning_handoff_saves_time_selected_status_preserves_existing_and_replays(self):
        body = await self.handoff_body()
        response = await self.client.post(self.base + "/places/import", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["prepared_for_planning"])
        selected_id = response.json()["destination_ids"][0]
        replay = await self.client.post(self.base + "/places/import", json=body)
        self.assertTrue(replay.json()["replayed"])
        async with self.sessions() as db:
            trip = await db.get(Trip, self.trip.id)
            self.assertEqual(trip.input_version, 2)
            self.assertEqual(trip.planned_start_at.hour, 3)
            self.assertEqual(trip.status, "active")
            selected = await db.get(TripDestination, selected_id)
            self.assertEqual(selected.visit_status, "must_visit")
            self.assertIsNone(selected.note)
            self.assertEqual((await db.get(TripDestination, self.destinations[0].id)).visit_status, "must_visit")
            self.assertEqual(await db.scalar(select(func.count()).select_from(TripDestination)), 3)

    async def test_handoff_requires_creator_workflow_and_complete_explicit_settings(self):
        body = await self.handoff_body()
        self.assertEqual((await self.other_client.post(self.base + "/places/import", json=body)).status_code, 403)
        incomplete = {**body, "planning_settings": None}
        self.assertEqual((await self.client.post(self.base + "/places/import", json=incomplete)).status_code, 422)
        async with self.sessions() as db:
            trip = await db.get(Trip, self.trip.id)
            trip.primary_workflow = "meeting"
            await db.commit()
        self.assertEqual((await self.client.post(self.base + "/places/import", json=body)).status_code, 409)

    async def test_invalid_handoff_rolls_back_new_place_status_and_settings(self):
        body = await self.handoff_body()
        async with self.sessions() as db:
            for item in self.destinations:
                destination = await db.get(TripDestination, item.id)
                destination.visit_status = "excluded"
            await db.commit()
        response = await self.client.post(self.base + "/places/import", json=body)
        self.assertEqual(response.status_code, 409, response.text)
        async with self.sessions() as db:
            trip = await db.get(Trip, self.trip.id)
            self.assertEqual(trip.input_version, 1)
            self.assertEqual(trip.planned_start_at, self.trip.planned_start_at)
            self.assertEqual(await db.scalar(select(func.count()).select_from(TripDestination)), 2)

    async def test_handoff_then_formal_route_uses_saved_inputs_without_final_confirmation(self):
        from app.routers import itineraries
        body = await self.handoff_body()
        prepared = await self.client.post(self.base + "/places/import", json=body)
        self.assertEqual(prepared.status_code, 200, prepared.text)
        async def calculate(participants, destinations, start, end, mode, objective):
            self.assertEqual(start.hour, 3)
            self.assertEqual(end.hour, 7)
            self.assertEqual(len(destinations), 3)
            return {"first_destination_id": destinations[0]["id"], "participant_arrivals": [],
                    "ordered_stops": [], "route_legs": [], "warnings": [],
                    "total_travel_min": 10, "total_duration_min": 60,
                    "group_start_at": start.isoformat(), "total_distance_km": 1}
        with patch.object(itineraries, "plan_itinerary", calculate):
            response = await self.client.post(self.base + "/itinerary-plans", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["is_stale"])
        self.assertEqual(response.json()["input_version"], 2)
        async with self.sessions() as db:
            self.assertEqual((await db.get(Trip, self.trip.id)).status, "active")

    async def test_return_handoff_persists_snapshot_and_estimated_confirmation_guard(self):
        from app.routers import itineraries
        from app.services import itinerary
        body = await self.handoff_body()
        body["planning_settings"]["return_location"] = {"lat":31.2, "lng":121.4, "address":"集合中心"}
        prepared = await self.client.post(self.base + "/places/import", json=body)
        self.assertEqual(prepared.status_code,200,prepared.text)
        loaded = await self.client.get(self.base + "/planning-settings")
        self.assertEqual(loaded.json()["return_location"]["address"],"集合中心")
        async def estimate(origin,dest,mode):
            return {"duration_min":5,"distance_km":1,"polyline":[[origin[1],origin[0]],[dest[1],dest[0]]],
                    "steps":[],"estimated":dest==(31.2,121.4),"fallback_reason":"offline" if dest==(31.2,121.4) else None}
        with patch.object(itinerary,"_route",estimate):
            plan = await self.client.post(self.base + "/itinerary-plans",json={})
        self.assertEqual(plan.status_code,200,plan.text)
        snapshot=plan.json()
        self.assertTrue(snapshot["return_leg"]["estimated"])
        self.assertEqual(snapshot["return_leg"]["return_location"]["address"],"集合中心")
        path=self.base+f"/itinerary-plans/{snapshot['plan_id']}/confirm"
        self.assertEqual((await self.client.post(path,json={})).status_code,409)
        confirmed=await self.client.post(path,json={"accept_estimated_routes":True})
        self.assertEqual(confirmed.status_code,200,confirmed.text)
        self.assertEqual(confirmed.json()["return_leg"],snapshot["return_leg"])

    async def test_return_target_changes_invalidate_plan_and_omitted_field_preserves_it(self):
        from app.services import itinerary
        body=await self.handoff_body()
        body["planning_settings"]["return_location"]={"lat":31.2,"lng":121.4,"address":"old"}
        await self.client.post(self.base+"/places/import",json=body)
        with patch.object(itinerary,"_route",self.fake_route):
            plan=await self.client.post(self.base+"/itinerary-plans",json={})
        settings=dict(body["planning_settings"])
        del settings["return_location"]
        legacy=await self.client.put(self.base+"/planning-settings",json=settings)
        self.assertEqual(legacy.json()["return_location"]["address"],"old")
        settings["return_location"]=None
        changed=await self.client.put(self.base+"/planning-settings",json=settings)
        self.assertEqual(changed.status_code,200,changed.text)
        self.assertIsNone(changed.json()["return_location"])
        latest=await self.client.get(self.base+"/itinerary-plans/latest")
        self.assertTrue(latest.json()["is_stale"])
        self.assertEqual(latest.json()["return_leg"]["return_location"]["address"],"old")
        self.assertEqual((await self.client.post(self.base+f"/itinerary-plans/{plan.json()['plan_id']}/confirm",json={})).status_code,409)

    async def test_agent_generation_preserves_return_target_and_snapshot(self):
        from app.routers import agent
        from app.services.agent_assistant import AgentAction
        from app.services import itinerary
        async with self.sessions() as db:
            trip=await db.get(Trip,self.trip.id)
            trip.return_location={"lat":31.2,"lng":121.4,"address":"Agent终点"}
            pid=await agent._store_proposal(db,self.trip.id,self.user.id,1,[AgentAction(type="generate_itinerary")])
            await db.commit()
        with patch.object(itinerary,"_route",self.fake_route):
            response=await self.client.post(self.base+"/assistant/proposals/execute",json={"proposal_id":pid})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["itinerary"]["return_leg"]["return_location"]["address"],"Agent终点")
        replay=await self.client.post(self.base+"/assistant/proposals/execute",json={"proposal_id":pid})
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["itinerary"]["return_leg"],response.json()["itinerary"]["return_leg"])
