"""B2 contract tests with mocked sessions only. No engine, DB writes or migrations."""
import importlib.util
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.models import TripDestination, TripPlaceImport
from app.routers import saved_places as route
from app.schemas import PlaceBatchImport


CONFIRMATION_ID = "660d097c-c55f-45c9-bf56-80c079b042a4"


def request(**changes):
    confirmation = {"confirmation_id": CONFIRMATION_ID, "confirmed": True, "selection_method": "manual",
                    "destination": {"name": "演示公园", "lat": 31.2, "lng": 121.4,
                                    "note": "合成备注", "expected_stay_min": 90}}
    return {"expected_version": 1, "places": [{"confirmation": confirmation}], **changes}


class ConfirmationSchemaTests(unittest.TestCase):
    def test_existing_direct_and_saved_requests_remain_valid(self):
        for item in [{"place": {"name": "演示", "lat": 31.2, "lng": 121.4}},
                     {"saved_place_id": 1, "revision": 2}]:
            self.assertEqual(len(PlaceBatchImport.model_validate({"expected_version": 1, "places": [item]}).places), 1)

    def test_explicit_confirmation_and_exactly_one_source_required(self):
        bad = []
        missing = request()
        del missing["places"][0]["confirmation"]["confirmed"]
        bad.append(missing)
        for key, value in [("confirmed", False), ("confirmation_id", "bad"), ("selection_method", "model")]:
            body = request()
            body["places"][0]["confirmation"][key] = value
            bad.append(body)
        duplicate = request()
        duplicate["places"][0]["saved_place_id"] = 1
        bad.append(duplicate)
        batch = request()
        batch["places"] *= 2
        bad.append(batch)
        planning = request(confirm_for_planning=True, planning_settings={})
        bad.append(planning)
        for body in bad:
            with self.subTest(body=body), self.assertRaises(ValidationError):
                PlaceBatchImport.model_validate(body)

    def test_nonfinite_coordinates_and_untrusted_extra_fields_are_rejected(self):
        for change in [{"lat": float("nan")}, {"lat": 91}, {"verification_status": "matched"}]:
            body = request()
            body["places"][0]["confirmation"]["destination"].update(change)
            with self.assertRaises(ValidationError):
                PlaceBatchImport.model_validate(body)

    def test_stable_identity_detects_edits_but_ignores_trip_version(self):
        first = PlaceBatchImport.model_validate(request())
        retry = PlaceBatchImport.model_validate(request(expected_version=8))
        self.assertEqual(route.confirmation_identity(first.places[0].confirmation),
                         route.confirmation_identity(retry.places[0].confirmation))
        changed = request()
        changed["places"][0]["confirmation"]["destination"]["name"] = "已编辑"
        old_key, old_hash = route.confirmation_identity(first.places[0].confirmation)
        key, fingerprint = route.confirmation_identity(PlaceBatchImport.model_validate(changed).places[0].confirmation)
        self.assertEqual(key, old_key)
        self.assertNotEqual(fingerprint, old_hash)

    def test_receipt_metadata_keeps_uniqueness_and_deletion_tombstones(self):
        table = TripPlaceImport.__table__
        constraint = next(c for c in table.constraints if c.name == "uq_trip_place_import")
        self.assertEqual([c.name for c in constraint.columns], ["trip_id", "user_id", "source_key"])
        self.assertTrue(table.c.destination_id.nullable)
        self.assertEqual(next(iter(table.c.destination_id.foreign_keys)).ondelete, "SET NULL")
        self.assertTrue(table.c.request_hash.nullable)


class ConfirmationRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.trip = SimpleNamespace(input_version=1, status="active", primary_workflow="itinerary")
        self.user = SimpleNamespace(id=7)
        self.receipt = None
        self.count = 0
        self.added = []
        self.order = []
        self.db = MagicMock()

        async def scalar(statement):
            self.order.append("read")
            return self.receipt if statement.column_descriptions[0]["entity"] is TripPlaceImport else self.count

        async def flush():
            for item in self.added:
                if isinstance(item, TripDestination) and item.id is None:
                    item.id = 42

        self.db.scalar = AsyncMock(side_effect=scalar)
        self.db.flush = AsyncMock(side_effect=flush)
        self.db.commit = AsyncMock()
        self.db.delete = AsyncMock()
        self.db.add.side_effect = self.added.append
        async def lock(*args):
            self.order.append("lock")
            return self.trip
        self.member = AsyncMock(return_value=SimpleNamespace(role="member"))
        self.active = AsyncMock(return_value=self.trip)
        self.broadcast = AsyncMock()
        for patcher in [patch.object(route, "require_trip_member", self.member),
                        patch.object(route, "lock_trip", AsyncMock(side_effect=lock)),
                        patch.object(route, "lock_trip_active", AsyncMock(side_effect=lock)),
                        patch.object(route, "require_trip_active", self.active),
                        patch.object(route.manager, "broadcast", self.broadcast)]:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def call(self, body=None):
        return await route.import_places(3, PlaceBatchImport.model_validate(body or request()), self.user, self.db)

    def existing(self, destination_id=42, request_hash=None):
        key, fingerprint = route.confirmation_identity(PlaceBatchImport.model_validate(request()).places[0].confirmation)
        self.receipt = SimpleNamespace(destination_id=destination_id, request_hash=request_hash or fingerprint, source_key=key)

    async def test_new_confirmation_uses_common_writer_preserves_all_fields_and_bumps_once(self):
        body = request()
        body["places"][0]["confirmation"]["destination"].update({
            "visit_window_start": "2026-11-01T13:00:00+08:00",
            "visit_window_end": "2026-11-01T14:00:00+08:00"})
        result = await self.call(body)
        self.assertEqual(result["destination_ids"], [42])
        self.assertFalse(result["replayed"])
        destination = next(item for item in self.added if isinstance(item, TripDestination))
        self.assertEqual(destination.note, "合成备注")
        self.assertEqual(destination.expected_stay_min, 90)
        self.assertEqual(destination.visit_window_start.hour, 13)
        receipt = next(item for item in self.added if isinstance(item, TripPlaceImport))
        self.assertEqual(len(receipt.request_hash), 64)
        self.assertEqual(self.order[:2], ["lock", "read"])
        self.active.assert_awaited_once()
        self.db.commit.assert_awaited_once()
        self.broadcast.assert_awaited_once()
        self.assertEqual(self.trip.input_version, 2)

    async def test_lost_response_retry_replays_without_new_write_version_or_broadcast(self):
        await self.call()
        self.receipt = next(item for item in self.added if isinstance(item, TripPlaceImport))
        self.db.commit.reset_mock()
        self.broadcast.reset_mock()
        self.active.reset_mock()
        self.added.clear()
        result = await self.call()  # Original expected_version=1 is deliberately stale.
        self.assertTrue(result["replayed"])
        self.assertEqual(result["destination_ids"], [42])
        self.assertEqual(result["input_version"], 2)
        self.assertEqual(self.added, [])
        self.db.commit.assert_not_awaited()
        self.broadcast.assert_not_awaited()
        self.active.assert_not_awaited()

    async def test_completed_request_can_be_read_after_trip_locked(self):
        self.existing()
        self.trip.status = "confirmed"
        self.active.side_effect = HTTPException(409, "locked")
        self.assertTrue((await self.call())["replayed"])
        self.active.assert_not_awaited()

    async def test_concurrent_requests_follow_serialized_writer_contract_with_fake_lock(self):
        # Exercises the application protocol only; it does not prove PostgreSQL locking.
        mutex = asyncio.Lock()
        async def lock(*args):
            await mutex.acquire()
            return self.trip
        async def commit():
            await asyncio.sleep(0)
            self.receipt = next(item for item in self.added if isinstance(item, TripPlaceImport))
        self.db.commit.side_effect = commit
        async def confirm():
            try:
                return await self.call()
            finally:
                mutex.release()
        with patch.object(route, "lock_trip", lock):
            results = await asyncio.gather(confirm(), confirm())
        self.assertEqual(sum(result["replayed"] for result in results), 1)
        self.assertEqual(len([x for x in self.added if isinstance(x, TripDestination)]), 1)
        self.assertEqual(self.trip.input_version, 2)
        self.db.commit.assert_awaited_once()

    async def test_same_identity_different_content_is_conflict(self):
        self.existing()
        body = request()
        body["places"][0]["confirmation"]["destination"]["note"] = "编辑后的备注"
        with self.assertRaises(HTTPException) as caught:
            await self.call(body)
        self.assertEqual(caught.exception.status_code, 409)
        self.db.add.assert_not_called()

    async def test_deleted_target_returns_gone_without_recreation(self):
        self.existing(destination_id=None)
        with self.assertRaises(HTTPException) as caught:
            await self.call()
        self.assertEqual(caught.exception.status_code, 410)
        self.db.add.assert_not_called()

    async def test_nonmember_denied_before_lock_or_read(self):
        self.member.side_effect = HTTPException(403, "not a member")
        with self.assertRaises(HTTPException) as caught:
            await self.call()
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(self.order, [])

    async def test_new_write_rejects_locked_trip_stale_version_and_capacity(self):
        self.active.side_effect = HTTPException(409, "locked")
        with self.assertRaises(HTTPException):
            await self.call()
        self.active.side_effect = None
        self.trip.input_version = 2
        with self.assertRaises(HTTPException):
            await self.call()
        self.trip.input_version = 1
        self.count = 10
        with self.assertRaises(HTTPException):
            await self.call()
        self.db.add.assert_not_called()
        self.db.commit.assert_not_awaited()

    async def test_failed_commit_does_not_broadcast_success(self):
        self.db.commit.side_effect = RuntimeError("synthetic commit failure")
        with self.assertRaises(RuntimeError):
            await self.call()
        self.broadcast.assert_not_awaited()

    async def test_old_direct_import_uses_old_key_and_excludes_private_notes(self):
        body = {"expected_version": 1, "places": [{"place": {
            "name": "演示公园", "lat": 31.2, "lng": 121.4, "private_note": "private"}}]}
        self.assertFalse((await self.call(body))["replayed"])
        dest = next(item for item in self.added if isinstance(item, TripDestination))
        self.assertIsNone(dest.note)
        receipt = next(item for item in self.added if isinstance(item, TripPlaceImport))
        self.assertIsNone(receipt.request_hash)

    async def test_old_deleted_receipt_is_replaced_on_legacy_reimport(self):
        self.receipt = SimpleNamespace(destination_id=None, request_hash=None)
        body = {"expected_version": 1, "places": [{"place": {"name": "演示", "lat": 31.2, "lng": 121.4}}]}
        self.assertFalse((await self.call(body))["replayed"])
        self.db.delete.assert_awaited_once_with(self.receipt)


class ConfirmationMigrationDefinitionTests(unittest.TestCase):
    def test_upgrade_and_guarded_downgrade_definitions_without_database(self):
        path = Path(__file__).resolve().parents[1] / "migrations/versions/0016_text_place_confirmation.py"
        spec = importlib.util.spec_from_file_location("confirmation_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        operations = MagicMock()
        with patch.object(module, "op", operations):
            module.upgrade()  # All operations are spies: no migration context or connection exists.
            self.assertEqual(operations.create_foreign_key.call_args.kwargs["ondelete"], "SET NULL")
            self.assertTrue(operations.alter_column.call_args.kwargs["nullable"])
            operations.reset_mock()
            module.downgrade()
            self.assertIn("RAISE EXCEPTION", operations.method_calls[0].args[0])
        self.assertEqual(module.down_revision, "0015_recovery_code")
