"""Agent execution regressions. Database tests use an isolated temporary schema.

Run real PostgreSQL tests explicitly with AGENT_DB_TESTS=1. They never use
existing users, token files, LLMs or map APIs. TEST_DATABASE_URL is optional.
"""
import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import event, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db import Base
from app.models import AgentConversationState, AgentProposal, ItineraryPlan, Trip, TripDestination, TripParticipant, User
from app.routers import agent
from app.schemas import AgentChatRequest, AgentProposalExecuteRequest, ParticipantUpdate
from app.services.agent_assistant import AgentAction, AgentAssistant, AgentModelAnswer, normalize_actions
from app.services.agent_state import claim_proposal, cleanup_agent_data, history_state, release_proposal
from app.services.planning_input import PlanningInput


class AgentExecutionUnitTests(unittest.TestCase):
    def test_all_destination_edits_survive_and_merge_in_order(self):
        actions = normalize_actions([
            AgentAction(type="generate_itinerary"),
            AgentAction(type="update_destination", destination_id=1, expected_stay_min=30),
            AgentAction(type="update_destination", destination_id=2, expected_stay_min=40),
            AgentAction(type="update_destination", destination_id=1, visit_status="optional"),
            AgentAction(type="generate_itinerary"),
        ])
        self.assertEqual([a.destination_id for a in actions[:-1]], [1, 2, 1])
        inputs = PlanningInput("itinerary", None, None, "transit", "balanced", [], [
            {"id": 1, "expected_stay_min": 60, "visit_status": "must_visit"},
            {"id": 2, "expected_stay_min": 60, "visit_status": "must_visit"},
        ])
        self.assertEqual(agent._project_actions(inputs, actions), 3)
        self.assertEqual(inputs.destinations[0]["expected_stay_min"], 30)
        self.assertEqual(inputs.destinations[0]["visit_status"], "optional")
        self.assertEqual(inputs.destinations[1]["expected_stay_min"], 40)

    def test_clear_window_requires_explicit_flag_and_rejects_conflicting_values(self):
        from pydantic import ValidationError
        for fields in ({}, {"visit_window_start": None, "visit_window_end": None}):
            with self.assertRaises(ValidationError):
                AgentAction(type="update_destination", destination_id=1, **fields)
        action = AgentAction(type="update_destination", destination_id=1, clear_visit_window=True)
        restored = AgentAction.model_validate(action.model_dump(mode="json"))
        self.assertTrue(restored.clear_visit_window)
        with self.assertRaises(ValidationError):
            AgentAction(type="generate_itinerary", clear_visit_window=True)
        with self.assertRaises(ValidationError):
            AgentAction(type="update_destination", destination_id=1, clear_visit_window=True,
                        visit_window_start=datetime.now(timezone.utc))
        inputs = PlanningInput("itinerary", None, None, "transit", "balanced", [], [
            {"id": 1, "visit_window_start": "start", "visit_window_end": "end", "expected_stay_min": 60}])
        agent._project_actions(inputs, [restored])
        self.assertIsNone(inputs.destinations[0]["visit_window_start"])
        self.assertIsNone(inputs.destinations[0]["visit_window_end"])
        self.assertEqual(inputs.destinations[0]["expected_stay_min"], 60)

    def test_clear_window_fallback_requires_unique_name_and_direct_instruction(self):
        context = {"trip": {}, "destinations": [{"id": 1, "name": "博物馆"}, {"id": 2, "name": "公园"}]}
        actions = AgentAssistant.infer_actions("取消博物馆的固定时间窗", context)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].destination_id, 1)
        self.assertTrue(actions[0].clear_visit_window)
        for message in ("不要取消博物馆的固定时间窗", "怎么取消博物馆的固定时间窗？",
                        "取消固定时间窗", "取消博物馆和公园的固定时间窗"):
            self.assertEqual(AgentAssistant.infer_actions(message, context), [])

    def test_unused_import_text_is_removed(self):
        actions = normalize_actions([AgentAction(type="generate_itinerary", import_text="secret")])
        self.assertIsNone(actions[0].import_text)

    def test_history_epoch_invalidates_other_workers_memory(self):
        assistant = AgentAssistant()
        assistant.sync_history_epoch(1, 2, "before")
        assistant.remember(1, 2, "secret", "answer")
        assistant.sync_history_epoch(1, 2, "before")
        self.assertTrue(assistant.history(1, 2))
        assistant.sync_history_epoch(1, 2, "after")
        self.assertEqual(assistant.history(1, 2), [])


@unittest.skipUnless(os.getenv("AGENT_DB_TESTS") == "1", "set AGENT_DB_TESTS=1 for isolated PostgreSQL tests")
class AgentExecutionDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url = os.getenv("TEST_DATABASE_URL", settings.database_url)
        self.schema = "agent_test_" + uuid4().hex
        self.admin = create_async_engine(url, echo=False)
        async with self.admin.begin() as conn:
            await conn.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        self.addAsyncCleanup(self.cleanup_schema)
        self.engine = create_async_engine(url, echo=False, connect_args={
            "server_settings": {"search_path": self.schema},
        })
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as db:
            self.user = User(name="isolated creator")
            self.other = User(name="isolated member")
            db.add_all([self.user, self.other])
            await db.flush()
            self.trip = Trip(title="isolated Agent test", creator_id=self.user.id, invite_code="TEST01",
                             planned_start_at=datetime(2026, 11, 1, 2, tzinfo=timezone.utc),
                             planned_end_at=datetime(2026, 11, 1, 10, tzinfo=timezone.utc))
            db.add(self.trip)
            await db.flush()
            db.add(TripParticipant(trip_id=self.trip.id, user_id=self.user.id, role="creator",
                                   start_location={"lat": 31.2, "lng": 121.4}))
            db.add(TripParticipant(trip_id=self.trip.id, user_id=self.other.id, role="member",
                                   start_location={"lat": 31.21, "lng": 121.41}))
            self.destinations = [TripDestination(
                trip_id=self.trip.id, submitted_by=self.user.id, name=name,
                lat=31.22 + i / 100, lng=121.42, visit_status="must_visit", expected_stay_min=60,
            ) for i, name in enumerate(["A", "B"])]
            db.add_all(self.destinations)
            await db.commit()

    async def cleanup_schema(self):
        try:
            # The only destructive operation targets the exact random schema
            # this test created. Never drop a database or any existing schema.
            assert self.schema.startswith("agent_test_") and len(self.schema) == 43
            async with self.admin.begin() as conn:
                await conn.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))
        finally:
            await self.admin.dispose()

    async def proposal(self, actions):
        async with self.sessions() as db:
            proposal_id = await agent._store_proposal(
                db, self.trip.id, self.user.id, 1, actions,
            )
            await db.commit()
            return proposal_id

    async def execute(self, proposal_id, user=None):
        async with self.sessions() as db:
            return await agent.execute_proposal(
                self.trip.id, AgentProposalExecuteRequest(proposal_id=proposal_id), user or self.user, db,
            )

    def edits(self, with_plan=False):
        actions = [AgentAction(type="update_destination", destination_id=d.id, expected_stay_min=30 + i * 10)
                   for i, d in enumerate(self.destinations)]
        return actions + ([AgentAction(type="generate_itinerary")] if with_plan else [])

    async def route_result(self, *args, **kwargs):
        return {"first_destination_id": self.destinations[0].id, "participant_arrivals": [],
                "ordered_stops": [], "route_legs": [], "warnings": [], "total_travel_min": 10,
                "total_duration_min": 80, "group_start_at": "2026-11-01T02:00:00Z", "total_distance_km": 2.5}

    async def test_clear_visit_window_persists_invalidates_old_version_and_replays(self):
        async with self.sessions() as db:
            destination = await db.get(TripDestination, self.destinations[0].id)
            destination.visit_window_start = self.trip.planned_start_at
            destination.visit_window_end = self.trip.planned_start_at + timedelta(hours=1)
            await db.commit()
        pid = await self.proposal([AgentAction(
            type="update_destination", destination_id=self.destinations[0].id, clear_visit_window=True)])
        async with self.sessions() as db:
            self.assertIsNotNone((await db.get(TripDestination, self.destinations[0].id)).visit_window_start)
        result = await self.execute(pid)
        self.assertTrue(result["executed"][0]["cleared_visit_window"])
        self.assertTrue((await self.execute(pid))["replayed"])
        async with self.sessions() as db:
            destination = await db.get(TripDestination, self.destinations[0].id)
            self.assertIsNone(destination.visit_window_start)
            self.assertIsNone(destination.visit_window_end)
            self.assertEqual(destination.expected_stay_min, 60)
            self.assertEqual((await db.get(Trip, self.trip.id)).input_version, 2)

    async def test_multiple_edits_plan_and_receipt_commit_once(self):
        pid = await self.proposal(self.edits(with_plan=True))
        with patch.object(agent, "plan_itinerary", self.route_result):
            first = await self.execute(pid)
            replay = await self.execute(pid)
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["itinerary"]["plan_id"], replay["itinerary"]["plan_id"])
        from app.services.agent_trace import read_traces
        self.assertNotEqual(first["trace_id"], replay["trace_id"])
        initial_trace = read_traces(self.user.id, self.trip.id, first["trace_id"])[0]
        replay_trace = read_traces(self.user.id, self.trip.id, replay["trace_id"])[0]
        self.assertEqual(initial_trace["proposal_id"], pid)
        self.assertEqual([s["stage"] for s in initial_trace["spans"]],
                         ["execution.prepare", "execution.commit"])
        self.assertTrue(replay_trace["replayed"])
        self.assertEqual(replay_trace["spans"], [])
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ItineraryPlan)), 1)
            trip = await db.get(Trip, self.trip.id)
            self.assertEqual(trip.input_version, 3)
            rows = (await db.scalars(select(TripDestination).order_by(TripDestination.id))).all()
            self.assertEqual([d.expected_stay_min for d in rows], [30, 40])
            proposal = await db.get(AgentProposal, pid)
            self.assertNotIn("trace_id", proposal.result)
            self.assertEqual(proposal.actions, [])
            self.assertEqual(proposal.status, "consumed")

    async def test_failure_before_commit_rolls_back_every_business_write(self):
        pid = await self.proposal(self.edits(with_plan=True))
        def fail_receipt_commit(session):
            if any(isinstance(obj, AgentProposal) and obj.status == "consumed" for obj in session.dirty):
                raise RuntimeError("injected before commit")
        async with self.sessions() as db:
            event.listen(db.sync_session, "before_commit", fail_receipt_commit)
            with patch.object(agent, "plan_itinerary", self.route_result):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    await agent.execute_proposal(self.trip.id, AgentProposalExecuteRequest(proposal_id=pid), self.user, db)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ItineraryPlan)), 0)
            self.assertEqual((await db.get(Trip, self.trip.id)).input_version, 1)
            rows = (await db.scalars(select(TripDestination))).all()
            self.assertEqual([d.expected_stay_min for d in rows], [60, 60])
            self.assertEqual((await db.get(AgentProposal, pid)).status, "pending")
        with patch.object(agent, "plan_itinerary", self.route_result):
            await self.execute(pid)

    async def test_external_failure_does_not_persist_projected_edits(self):
        pid = await self.proposal(self.edits(with_plan=True))
        async def fail(*args, **kwargs):
            raise RuntimeError("map unavailable")
        with patch.object(agent, "plan_itinerary", fail):
            with self.assertRaises(RuntimeError):
                await self.execute(pid)
        async with self.sessions() as db:
            self.assertEqual((await db.get(TripDestination, self.destinations[0].id)).expected_stay_min, 60)
            self.assertEqual((await db.get(AgentProposal, pid)).status, "pending")

    async def test_wrong_user_does_not_consume_proposal(self):
        pid = await self.proposal(self.edits())
        with self.assertRaises(HTTPException) as caught:
            await self.execute(pid, self.other)
        self.assertEqual(caught.exception.status_code, 403)
        await self.execute(pid)

    async def test_concurrent_claims_have_one_winner(self):
        pid = await self.proposal(self.edits())
        async def claim():
            async with self.sessions() as db:
                value = await claim_proposal(db, pid, self.trip.id, self.user.id)
                await db.commit()
                return value
        values = await asyncio.gather(claim(), claim())
        self.assertEqual(sum(value is not None for value in values), 1)

    async def test_lost_worker_reclaimed_and_old_token_cannot_release(self):
        pid = await self.proposal(self.edits())
        async with self.sessions() as db:
            value = await claim_proposal(db, pid, self.trip.id, self.user.id)
            old_token = value.lease_token
            await db.commit()
        # A new session simulates a restarted process after its lease elapsed.
        async with self.sessions() as db:
            await db.execute(update(AgentProposal).where(AgentProposal.id == pid).values(
                lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
            await db.commit()
        async with self.sessions() as db:
            value = await claim_proposal(db, pid, self.trip.id, self.user.id)
            self.assertNotEqual(value.lease_token, old_token)
            new_token = value.lease_token
            await db.commit()
            await release_proposal(db, pid, old_token)
            await db.commit()
        async with self.sessions() as db:
            self.assertEqual((await db.get(AgentProposal, pid)).lease_token, new_token)

    async def test_trip_edit_during_calculation_invalidates_all_actions(self):
        pid = await self.proposal(self.edits(with_plan=True))
        async def concurrent_edit(*args, **kwargs):
            from app.routers.participants import update_me
            async with self.sessions() as db:
                await update_me(self.trip.id, ParticipantUpdate(
                    start_location={"lat": 31.3, "lng": 121.3}, transport_mode="driving",
                ), self.user, db)
            return await self.route_result()
        with patch.object(agent, "plan_itinerary", concurrent_edit):
            with self.assertRaises(HTTPException) as caught:
                await self.execute(pid)
        self.assertEqual(caught.exception.status_code, 409)
        async with self.sessions() as db:
            self.assertEqual((await db.get(TripDestination, self.destinations[0].id)).expected_stay_min, 60)
            self.assertEqual((await db.get(AgentProposal, pid)).actions, [])
            self.assertEqual(await db.scalar(select(func.count()).select_from(ItineraryPlan)), 0)

    async def test_clear_revokes_inflight_execution(self):
        pid = await self.proposal(self.edits(with_plan=True))
        async def clear_during_computation(*args, **kwargs):
            async with self.sessions() as db:
                await agent.clear_history(self.trip.id, self.user, db)
            return await self.route_result()
        with patch.object(agent, "plan_itinerary", clear_during_computation):
            with self.assertRaises(HTTPException):
                await self.execute(pid)
        async with self.sessions() as db:
            proposal = await db.get(AgentProposal, pid)
            self.assertEqual(proposal.status, "cancelled")
            self.assertEqual(proposal.actions, [])
            self.assertEqual(await db.scalar(select(func.count()).select_from(ItineraryPlan)), 0)

    async def test_expired_import_scrubbed_and_consumed_import_not_retained(self):
        raw = "PRIVATE ORIGINAL TEXT"
        pid = await self.proposal([AgentAction(type="prepare_place_import", import_text=raw)])
        async with self.sessions() as db:
            await db.execute(update(AgentProposal).where(AgentProposal.id == pid).values(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
            await db.commit()
            await cleanup_agent_data(db)
            await db.commit()
            self.assertEqual((await db.get(AgentProposal, pid)).actions, [])
        with self.assertRaises(HTTPException):
            await self.execute(pid)
        pid = await self.proposal([AgentAction(type="prepare_place_import", import_text=raw)])
        async def parse(*args, **kwargs):
            return {"candidates": [{"name": "A", "reason": raw}], "comment_summary": {"raw": raw}}
        with patch.object(agent, "parse_text", parse):
            first = await self.execute(pid)
        self.assertIn(raw, str(first))
        async with self.sessions() as db:
            proposal = await db.get(AgentProposal, pid)
            self.assertEqual(proposal.actions, [])
            self.assertNotIn(raw, str(proposal.result))
        replay = await self.execute(pid)
        self.assertTrue(replay["replayed"])
        self.assertIsNone(replay["place_import"])
        self.assertIn("warning", replay)

    async def test_first_history_upserts_concurrently_without_unique_conflict(self):
        async def initial():
            async with self.sessions() as db:
                value = await history_state(db, self.trip.id, self.user.id)
                epoch = value.history_epoch
                await db.commit()
                return epoch
        epochs = await asyncio.gather(initial(), initial())
        self.assertEqual(epochs[0], epochs[1])
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(AgentConversationState)), 1)

    async def test_expiry_during_computation_prevents_commit_and_scrubs_payload(self):
        pid = await self.proposal(self.edits(with_plan=True))
        async def expire(*args, **kwargs):
            async with self.sessions() as db:
                await db.execute(update(AgentProposal).where(AgentProposal.id == pid).values(
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
                await db.commit()
            return await self.route_result()
        with patch.object(agent, "plan_itinerary", expire):
            with self.assertRaises(HTTPException):
                await self.execute(pid)
        async with self.sessions() as db:
            proposal = await db.get(AgentProposal, pid)
            self.assertEqual(proposal.status, "expired")
            self.assertEqual(proposal.actions, [])
            self.assertEqual((await db.get(Trip, self.trip.id)).input_version, 1)

    async def test_stale_worker_cannot_commit_after_new_worker_takes_over(self):
        pid = await self.proposal(self.edits(with_plan=True))
        started, resume = asyncio.Event(), asyncio.Event()
        calls = 0
        async def delayed_route(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await resume.wait()
            return await self.route_result()
        with patch.object(agent, "plan_itinerary", delayed_route):
            old_worker = asyncio.create_task(self.execute(pid))
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                async with self.sessions() as db:
                    await db.execute(update(AgentProposal).where(AgentProposal.id == pid).values(
                        lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
                    await db.commit()
                winner = await self.execute(pid)
                resume.set()
                with self.assertRaises(HTTPException):
                    await asyncio.wait_for(old_worker, timeout=5)
            finally:
                resume.set()
                if not old_worker.done():
                    old_worker.cancel()
                await asyncio.gather(old_worker, return_exceptions=True)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ItineraryPlan)), 1)
            receipt = (await db.get(AgentProposal, pid)).result
            self.assertEqual(receipt["itinerary"]["plan_id"], winner["itinerary"]["plan_id"])

    async def test_migrations_upgrade_legacy_data_and_roundtrip(self):
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import inspect
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            def migrate(conn):
                config = Config("alembic.ini")
                config.attributes["connection"] = conn
                command.upgrade(config, "0009_agent_state")
                # Seed a legacy consumed/raw record and an uncertain executing
                # record: neither may be retried or retain its original text.
                conn.execute(text("INSERT INTO users (id, name) VALUES (1, 'migration test')"))
                conn.execute(text("INSERT INTO trips (id, title, creator_id, invite_code) VALUES (1, 'test', 1, 'MIG001')"))
                conn.execute(text("""INSERT INTO agent_proposals
                    (id, trip_id, user_id, input_version, actions, status, expires_at)
                    VALUES ('legacy', 1, 1, 1, '[{"import_text":"private"}]', 'executing', now() + interval '10 minutes')"""))
                command.upgrade(config, "head")
                row = conn.execute(text("SELECT actions, status FROM agent_proposals WHERE id='legacy'")).one()
                self.assertEqual(row.actions, [])
                self.assertEqual(row.status, "expired")
                self.assertIn("lease_token", {c["name"] for c in inspect(conn).get_columns("agent_proposals")})
                self.assertIn("account_recovery_codes", inspect(conn).get_table_names())
                self.assertEqual(conn.scalar(text("SELECT count(*) FROM account_recovery_codes")), 0)
                conn.execute(text("INSERT INTO account_recovery_codes (user_id, code_hash) VALUES (1, :digest)"), {"digest": "a" * 64})
                command.downgrade(config, "0009_agent_state")
                self.assertNotIn("account_recovery_codes", inspect(conn).get_table_names())
                command.upgrade(config, "head")
                self.assertEqual(conn.scalar(text("SELECT version_num FROM alembic_version")), "0015_recovery_code")
                self.assertEqual(conn.scalar(text("SELECT count(*) FROM account_recovery_codes")), 0)
                self.assertEqual(conn.scalar(text("SELECT count(*) FROM users WHERE id=1")), 1)
            await connection.run_sync(migrate)

    async def test_http_execute_replay_confirm_reopen_and_clear(self):
        import httpx
        from app.db import get_db
        from app.main import app
        from app.security import get_current_user
        async def isolated_db():
            async with self.sessions() as db:
                yield db
        async def current_user():
            return self.user
        previous = dict(app.dependency_overrides)
        app.dependency_overrides[get_db] = isolated_db
        app.dependency_overrides[get_current_user] = current_user
        try:
            pid = await self.proposal(self.edits(with_plan=True))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                path = f"/trips/{self.trip.id}"
                with patch.object(agent, "plan_itinerary", self.route_result):
                    first = await client.post(path + "/assistant/proposals/execute", json={"proposal_id": pid})
                    self.assertEqual(first.status_code, 200, first.text)
                    replay = await client.post(path + "/assistant/proposals/execute", json={"proposal_id": pid})
                self.assertTrue(replay.json()["replayed"])
                trace_path = path + "/assistant/traces/" + first.json()["trace_id"]
                observed = await client.get(trace_path)
                self.assertEqual(observed.status_code, 200, observed.text)
                self.assertEqual(observed.json()["proposal_id"], pid)
                async def other_user():
                    return self.other
                app.dependency_overrides[get_current_user] = other_user
                self.assertEqual((await client.get(trace_path)).status_code, 404)
                app.dependency_overrides[get_current_user] = current_user
                plan_id = first.json()["itinerary"]["plan_id"]
                confirmed = await client.post(path + f"/itinerary-plans/{plan_id}/confirm", json={})
                self.assertEqual(confirmed.status_code, 200, confirmed.text)
                replay_locked = await client.post(path + "/assistant/proposals/execute", json={"proposal_id": pid})
                self.assertTrue(replay_locked.json()["replayed"])
                rejected = await client.put(path + "/planning-settings", json={
                    "planned_start_at": "2026-11-01T02:00:00Z", "planned_end_at": "2026-11-01T10:00:00Z",
                    "group_transport_mode": "transit", "optimization_objective": "balanced",
                })
                self.assertEqual(rejected.status_code, 409)
                reopened = await client.post(path + "/reopen")
                self.assertEqual(reopened.status_code, 200)
                self.assertEqual(reopened.json()["input_version"], 4)
                cleared = await client.delete(path + "/assistant/history")
                self.assertEqual(cleared.json(), {"cleared": True})
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)

    async def test_chat_response_after_clear_cannot_restore_proposal(self):
        async def answer(*args, **kwargs):
            async with self.sessions() as db:
                await agent.clear_history(self.trip.id, self.user, db)
            return AgentModelAnswer(reply="old reply", actions=self.edits()), False, None
        async with self.sessions() as db:
            with patch.object(agent.agent_assistant, "chat", answer):
                result = await agent.chat(self.trip.id, AgentChatRequest(message="test"), self.user, db)
        self.assertIsNone(result["proposal_id"])
        self.assertEqual(result["actions"], [])
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(AgentProposal)), 0)


if __name__ == "__main__":
    unittest.main()
