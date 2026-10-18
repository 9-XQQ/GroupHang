"""Explicit P0-B acceptance only; provisions new databases, never a business schema."""
import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
import httpx
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import db as application_db
from app.db import get_db
from app.models import Trip, TripDestination, TripParticipant, TripPlaceImport, User
from app.routers import destinations, saved_places
from app.security import get_current_user
from tests.p0b_isolated_database import IsolatedP0BDatabase


@unittest.skipUnless(os.getenv("P0B_DB_TESTS") == "1", "set P0B_DB_TESTS=1 to provision dedicated local test databases")
class P0BDatabaseIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        def forbid_default_engine(*args):
            raise AssertionError("Default business engine is forbidden during P0-B acceptance")
        event.listen(application_db.engine.sync_engine, "do_connect", forbid_default_engine)
        cls.addClassCleanup(event.remove, application_db.engine.sync_engine, "do_connect", forbid_default_engine)
        cls.resource = IsolatedP0BDatabase()
        cls.addClassCleanup(lambda: asyncio.run(cls.resource.cleanup()))
        asyncio.run(cls.resource.prepare())
        print("P0-B isolation verified:", cls.resource.name, "empty at creation; restricted role; synthetic data only")

    async def asyncSetUp(self):
        self.engine = self.resource.engine()
        self.addAsyncCleanup(self.engine.dispose)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.connect() as conn:
            await self.resource.assert_target(conn)
        self.guard = patch("httpx.AsyncHTTPTransport.handle_async_request",
                           new=AsyncMock(side_effect=AssertionError("External HTTP forbidden")))
        self.guard.start()
        self.addCleanup(self.guard.stop)
        async with self.sessions() as db:
            self.creator = User(name="synthetic creator")
            self.member = User(name="synthetic member")
            self.outside = User(name="synthetic outsider")
            db.add_all([self.creator, self.member, self.outside])
            await db.flush()
            self.trip = Trip(title="synthetic P0-B acceptance", creator_id=self.creator.id,
                             invite_code=uuid4().hex[:8], input_version=1)
            db.add(self.trip)
            await db.flush()
            db.add_all([TripParticipant(trip_id=self.trip.id, user_id=self.creator.id, role="creator"),
                        TripParticipant(trip_id=self.trip.id, user_id=self.member.id, role="member")])
            await db.commit()
        self.app = FastAPI()
        self.app.include_router(saved_places.router)
        self.app.include_router(destinations.router)
        async def isolated_db():
            async with self.sessions() as db:
                await self.resource.assert_target(db)
                yield db
        users = {u.id: u for u in (self.creator, self.member, self.outside)}
        async def actor(x_test_actor: int = Header()):
            if x_test_actor not in users:
                raise HTTPException(401, "synthetic test user required")
            return users[x_test_actor]
        self.app.dependency_overrides[get_db] = isolated_db
        self.app.dependency_overrides[get_current_user] = actor
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
                                       base_url="http://isolated.test", headers={"X-Test-Actor": str(self.creator.id)})
        self.addAsyncCleanup(self.client.aclose)
        self.base = f"/trips/{self.trip.id}"

    def body(self):
        return {"expected_version": 1, "places": [{"confirmation": {
            "confirmation_id": str(uuid4()), "confirmed": True, "selection_method": "manual",
            "destination": {"name": "edited synthetic park", "address": "synthetic road 18",
                "lat": 31.234, "lng": 121.456, "category": "park", "note": "edited synthetic note",
                "expected_stay_min": 90, "visit_window_start": "2026-11-01T13:00:00+08:00",
                "visit_window_end": "2026-11-01T14:30:00+08:00"}}}]}

    async def counts(self):
        async with self.sessions() as db:
            destinations_count = await db.scalar(select(func.count()).select_from(TripDestination).where(TripDestination.trip_id == self.trip.id))
            receipts = await db.scalar(select(func.count()).select_from(TripPlaceImport).where(TripPlaceImport.trip_id == self.trip.id))
            trip = await db.get(Trip, self.trip.id)
            return destinations_count, receipts, trip.input_version

    async def test_first_confirmation_persists_edited_fields_and_duplicate_retry_is_read_only(self):
        body = self.body()
        first = await self.client.post(self.base + "/places/import", json=body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertFalse(first.json()["replayed"])
        with patch.object(saved_places.manager, "broadcast", AsyncMock()) as broadcast:
            second = await self.client.post(self.base + "/places/import", json=body)
            self.assertEqual(second.status_code, 200, second.text)
            broadcast.assert_not_awaited()
        self.assertTrue(second.json()["replayed"])
        self.assertEqual(first.json()["destination_ids"], second.json()["destination_ids"])
        self.assertEqual(await self.counts(), (1, 1, 2))
        async with self.sessions() as db:
            place = await db.get(TripDestination, first.json()["destination_ids"][0])
            self.assertEqual((place.name, place.address, place.category, place.note),
                ("edited synthetic park", "synthetic road 18", "park", "edited synthetic note"))
            self.assertAlmostEqual(place.lat, 31.234)
            self.assertAlmostEqual(place.lng, 121.456)
            self.assertEqual(place.expected_stay_min, 90)
            self.assertEqual(place.visit_window_start.isoformat(), "2026-11-01T05:00:00+00:00")
            self.assertEqual(place.visit_window_end.isoformat(), "2026-11-01T06:30:00+00:00")
            self.assertEqual(place.visit_status, "candidate")

    async def test_same_identity_changed_content_returns_409(self):
        body = self.body()
        self.assertEqual((await self.client.post(self.base + "/places/import", json=body)).status_code, 200)
        body["places"][0]["confirmation"]["destination"]["name"] = "changed after first confirmation"
        changed = await self.client.post(self.base + "/places/import", json=body)
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(await self.counts(), (1, 1, 2))

    async def test_deleted_destination_retains_receipt_and_returns_410(self):
        body = self.body()
        first = await self.client.post(self.base + "/places/import", json=body)
        place_id = first.json()["destination_ids"][0]
        removed = await self.client.delete(self.base + f"/destinations/{place_id}")
        self.assertEqual(removed.status_code, 204, removed.text)
        retry = await self.client.post(self.base + "/places/import", json=body)
        self.assertEqual(retry.status_code, 410, retry.text)
        self.assertEqual(await self.counts(), (0, 1, 3))
        async with self.sessions() as db:
            receipt = (await db.execute(select(TripPlaceImport).where(TripPlaceImport.trip_id == self.trip.id))).scalar_one()
            self.assertIsNone(receipt.destination_id)
            self.assertIsNotNone(receipt.request_hash)

    async def test_two_concurrent_confirmations_create_one_row_and_bump_once(self):
        body = self.body()
        with patch.object(saved_places.manager, "broadcast", AsyncMock()) as broadcast:
            responses = await asyncio.gather(*[self.client.post(self.base + "/places/import", json=body) for _ in range(2)])
            self.assertEqual([r.status_code for r in responses], [200, 200], [r.text for r in responses])
            self.assertEqual(sum(r.json()["replayed"] for r in responses), 1)
            broadcast.assert_awaited_once()
        self.assertEqual(await self.counts(), (1, 1, 2))

    async def test_database_failure_after_destination_insert_rolls_back_entire_transaction(self):
        # A real PostgreSQL trigger fails receipt insertion, after destination.flush().
        async with self.engine.begin() as conn:
            await conn.execute(text("""CREATE FUNCTION p0b_reject_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'synthetic receipt failure'; END $$"""))
            await conn.execute(text("""CREATE TRIGGER p0b_reject_receipt BEFORE INSERT ON trip_place_imports
                FOR EACH ROW EXECUTE FUNCTION p0b_reject_receipt()"""))
        try:
            with patch.object(saved_places.manager, "broadcast", AsyncMock()) as broadcast:
                result = await self.client.post(self.base + "/places/import", json=self.body())
                self.assertEqual(result.status_code, 500)
                broadcast.assert_not_awaited()
            self.assertEqual(await self.counts(), (0, 0, 1))
        finally:
            async with self.engine.begin() as conn:
                await conn.execute(text("DROP TRIGGER p0b_reject_receipt ON trip_place_imports"))
                await conn.execute(text("DROP FUNCTION p0b_reject_receipt()"))

    async def test_permissions_locked_trip_and_version_conflicts(self):
        body = self.body()
        forbidden = await self.client.post(self.base + "/places/import", json=body,
                                          headers={"X-Test-Actor": str(self.outside.id)})
        self.assertEqual(forbidden.status_code, 403)
        stale = {**body, "expected_version": 999}
        self.assertEqual((await self.client.post(self.base + "/places/import", json=stale)).status_code, 409)
        for state in ("confirmed", "completed"):
            async with self.sessions() as db:
                trip = await db.get(Trip, self.trip.id)
                trip.status = state
                await db.commit()
            self.assertEqual((await self.client.post(self.base + "/places/import", json=body)).status_code, 409)
        self.assertEqual(await self.counts(), (0, 0, 1))

    async def test_member_can_confirm_and_completed_request_replays_after_trip_locked(self):
        body = self.body()
        response = await self.client.post(self.base + "/places/import", json=body, headers={"X-Test-Actor": str(self.member.id)})
        self.assertEqual(response.status_code, 200, response.text)
        async with self.sessions() as db:
            trip = await db.get(Trip, self.trip.id)
            trip.status = "confirmed"
            await db.commit()
        retry = await self.client.post(self.base + "/places/import", json=body, headers={"X-Test-Actor": str(self.member.id)})
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertTrue(retry.json()["replayed"])
        self.assertEqual(await self.counts(), (1, 1, 2))

    async def test_manual_add_and_legacy_direct_saved_import_and_reimport_still_work(self):
        manual = await self.client.post(self.base + "/destinations", json={"name": "synthetic manual", "lat": 31.2, "lng": 121.4})
        self.assertEqual(manual.status_code, 201, manual.text)
        direct = {"expected_version": 2, "places": [{"place": {"name": "synthetic direct", "lat": 31.3, "lng": 121.5}}]}
        first = await self.client.post(self.base + "/places/import", json=direct)
        self.assertEqual(first.status_code, 200, first.text)
        retry = await self.client.post(self.base + "/places/import", json=direct)
        self.assertTrue(retry.json()["replayed"])
        saved = await self.client.post("/users/me/saved-places", json={"name": "synthetic saved", "lat": 31.4, "lng": 121.6,
                                                                          "private_note": "synthetic private memory"})
        self.assertEqual(saved.status_code, 201, saved.text)
        saved_body = {"expected_version": 3, "places": [{"saved_place_id": saved.json()["id"], "revision": 1}]}
        imported = await self.client.post(self.base + "/places/import", json=saved_body)
        self.assertEqual(imported.status_code, 200, imported.text)
        place_id = imported.json()["destination_ids"][0]
        async with self.sessions() as db:
            self.assertIsNone((await db.get(TripDestination, place_id)).note)
        self.assertEqual((await self.client.delete(self.base + f"/destinations/{place_id}")).status_code, 204)
        saved_body["expected_version"] = 5
        reimported = await self.client.post(self.base + "/places/import", json=saved_body)
        self.assertEqual(reimported.status_code, 200, reimported.text)
        self.assertNotEqual(reimported.json()["destination_ids"], [place_id])
        self.assertEqual(await self.counts(), (3, 2, 6))

    async def test_unique_constraint_and_foreign_key_are_enforced_by_postgres(self):
        body = self.body()
        result = await self.client.post(self.base + "/places/import", json=body)
        place_id = result.json()["destination_ids"][0]
        async with self.sessions() as db:
            original = (await db.execute(select(TripPlaceImport).where(TripPlaceImport.trip_id == self.trip.id))).scalar_one()
            key, fingerprint = original.source_key, original.request_hash
        with self.assertRaises(IntegrityError):
            async with self.sessions() as db:
                db.add(TripPlaceImport(trip_id=self.trip.id, user_id=self.creator.id, source_key=key,
                                      request_hash=fingerprint, destination_id=place_id))
                await db.commit()
        with self.assertRaises(IntegrityError):
            async with self.sessions() as db:
                db.add(TripPlaceImport(trip_id=self.trip.id, user_id=self.creator.id, source_key=uuid4().hex,
                                      request_hash=fingerprint, destination_id=9223372036854775807))
                await db.commit()
        self.assertEqual(await self.counts(), (1, 1, 2))

    async def test_migration_preserves_legacy_rows_and_matches_model_constraints(self):
        legacy = self.resource.audit["legacy_receipt"]
        async with self.engine.connect() as conn:
            row = (await conn.execute(text("SELECT destination_id,request_hash FROM trip_place_imports WHERE source_key=:key"),
                                      {"key": legacy["source_key"]})).one()
            self.assertEqual(row, (legacy["destination_id"], None))
            constraint = (await conn.execute(text("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
                WHERE conrelid='trip_place_imports'::regclass AND conname='trip_place_imports_destination_id_fkey'"""))).scalar_one()
            self.assertIn("ON DELETE SET NULL", constraint)
            unique = (await conn.execute(text("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
                WHERE conrelid='trip_place_imports'::regclass AND conname='uq_trip_place_import'"""))).scalar_one()
            self.assertEqual(unique, "UNIQUE (trip_id, user_id, source_key)")
            version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            self.assertEqual(version, "0016_text_place_confirmation")
            columns = (await conn.execute(text("""SELECT column_name,is_nullable FROM information_schema.columns
                WHERE table_schema='public' AND table_name='trip_place_imports'
                AND column_name IN ('request_hash','destination_id')"""))).all()
            self.assertEqual(dict(columns), {"request_hash": "YES", "destination_id": "YES"})

    async def test_new_orm_requires_migration_and_does_not_silently_accept_0015(self):
        engine = self.resource.engine(self.resource.restore_name)
        try:
            with self.assertRaises(DBAPIError) as caught:
                async with engine.connect() as conn:
                    await self.resource.assert_target(conn, self.resource.restore_name)
                    await conn.execute(select(TripPlaceImport))
            self.assertEqual(caught.exception.orig.sqlstate, "42703")  # Undefined column, request_hash.
            async with engine.connect() as conn:
                self.assertEqual((await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one(), "0015_recovery_code")
                self.assertEqual((await conn.execute(text("SELECT count(*) FROM trip_place_imports"))).scalar_one(), 1)
        finally:
            await engine.dispose()

    async def test_downgrade_refuses_to_destroy_confirmation_receipts(self):
        self.assertEqual((await self.client.post(self.base + "/places/import", json=self.body())).status_code, 200)
        with self.assertRaises(DBAPIError):
            await self.resource.migrate("0015_recovery_code", direction="downgrade")
        async with self.engine.connect() as conn:
            self.assertEqual((await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one(), "0016_text_place_confirmation")
        self.assertEqual(await self.counts(), (1, 1, 2))
