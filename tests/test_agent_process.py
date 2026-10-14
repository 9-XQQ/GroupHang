"""Real OS-process crash recovery and standalone retention acceptance tests."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, update

from app.config import settings
from app.models import AgentProposal, ItineraryPlan, Trip, TripDestination
from app.services.agent_assistant import AgentAction
from scripts.cleanup_agent_state import inspect_status
from tests import test_agent_execution as fixtures


class RetentionStatusTests(unittest.TestCase):
    def test_stale_missing_and_failed_heartbeats_are_unhealthy(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "status.json"
            self.assertEqual(inspect_status(path)[1], 1)
            path.write_text(json.dumps({
                "last_success_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                "consecutive_failures": 0,
            }), encoding="utf-8")
            self.assertFalse(inspect_status(path)[0]["healthy"])
            path.write_text(json.dumps({
                "last_success_at": datetime.now(timezone.utc).isoformat(), "consecutive_failures": 1,
            }), encoding="utf-8")
            self.assertEqual(inspect_status(path)[1], 1)


@unittest.skipUnless(os.getenv("AGENT_DB_TESTS") == "1", "set AGENT_DB_TESTS=1 for isolated subprocess tests")
class AgentProcessTests(unittest.IsolatedAsyncioTestCase):
    # Reuse fixture setup only, without inheriting and rerunning its test methods.
    cleanup_schema = fixtures.AgentExecutionDatabaseTests.cleanup_schema
    proposal = fixtures.AgentExecutionDatabaseTests.proposal
    edits = fixtures.AgentExecutionDatabaseTests.edits

    async def asyncSetUp(self):
        await fixtures.AgentExecutionDatabaseTests.asyncSetUp(self)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.workspace = Path(__file__).resolve().parent.parent

    async def stop_child(self, child):
        if child.poll() is None:
            child.kill()
        await asyncio.to_thread(child.wait, timeout=10)

    def start_child(self, mode, pid=None):
        stage = Path(self.folder.name) / (mode + "_" + str(len(list(Path(self.folder.name).iterdir()))) + ".json")
        status = Path(self.folder.name) / "retention_status.json"
        args = [sys.executable, "-m", "tests.agent_process_worker",
                "--mode", mode, "--schema", self.schema, "--stage-file", str(stage),
                "--status-file", str(status)]
        if pid:
            args += ["--proposal-id", pid, "--trip-id", str(self.trip.id),
                     "--user-id", str(self.user.id), "--destination-id", str(self.destinations[0].id)]
        env = {**os.environ, "DEBUG": "false", "PYTHONIOENCODING": "utf-8",
               "DATABASE_URL": os.getenv("TEST_DATABASE_URL", settings.database_url),
               "AMAP_API_KEY": "", "LLM_API_KEY": ""}
        child = subprocess.Popen(args, cwd=self.workspace, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.addAsyncCleanup(self.stop_child, child)
        return child, stage, status

    async def wait_stage(self, child, path, expected):
        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline:
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                if state["stage"] == expected:
                    return state
                if state["stage"] == "failed":
                    self.fail("child failed: " + state["error_type"])
            except (OSError, ValueError):
                pass
            if child.poll() is not None:
                self.fail("child exited before stage: " + expected)
            await asyncio.sleep(0.05)
        self.fail("child timed out before stage: " + expected)

    async def wait_real_lease_expiry(self, pid):
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            async with self.sessions() as db:
                clock, lease = (await db.execute(select(
                    func.clock_timestamp(), AgentProposal.lease_until,
                ).where(AgentProposal.id == pid))).one()
                if lease <= clock:
                    return
            await asyncio.sleep(0.05)
        self.fail("fixture lease did not expire")

    async def verify_crash_then_recover(self, mode):
        pid = await self.proposal(self.edits(with_plan=True))
        child, stage, _ = self.start_child(mode, pid)
        await self.wait_stage(child, stage, mode)
        await self.stop_child(child)
        self.assertIsNotNone(child.returncode)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ItineraryPlan)), 0)
            self.assertEqual((await db.get(Trip, self.trip.id)).input_version, 1)
            self.assertEqual((await db.get(TripDestination, self.destinations[0].id)).expected_stay_min, 60)
            self.assertEqual((await db.get(AgentProposal, pid)).status, "executing")
        await self.wait_real_lease_expiry(pid)
        restarted, completed, _ = self.start_child("execute", pid)
        response = await self.wait_stage(restarted, completed, "completed")
        await asyncio.to_thread(restarted.wait, timeout=10)
        self.assertEqual(restarted.returncode, 0)
        self.assertTrue(response["result"]["itinerary"]["plan_id"])
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ItineraryPlan)), 1)
            self.assertEqual((await db.get(Trip, self.trip.id)).input_version, 3)
            self.assertEqual((await db.get(AgentProposal, pid)).status, "consumed")

    async def test_kill_during_calculation_then_restart_recovers(self):
        await self.verify_crash_then_recover("preparing")

    async def test_kill_with_uncommitted_business_writes_rolls_back_then_recovers(self):
        await self.verify_crash_then_recover("before_commit")

    async def test_kill_after_commit_replays_receipt_without_duplicate_plan(self):
        pid = await self.proposal(self.edits(with_plan=True))
        child, stage, _ = self.start_child("after_commit", pid)
        first = await self.wait_stage(child, stage, "after_commit")
        await self.stop_child(child)
        restarted, completed, _ = self.start_child("execute", pid)
        replay = await self.wait_stage(restarted, completed, "completed")
        await asyncio.to_thread(restarted.wait, timeout=10)
        self.assertTrue(replay["result"]["replayed"])
        self.assertEqual(replay["result"]["itinerary"]["plan_id"], first["plan_id"])
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ItineraryPlan)), 1)
            self.assertEqual((await db.get(Trip, self.trip.id)).input_version, 3)

    async def test_standalone_worker_cleans_with_no_web_server_and_detects_failure(self):
        raw = "DISPOSABLE PRIVATE IMPORT"
        pid = await self.proposal([AgentAction(type="prepare_place_import", import_text=raw)])
        async with self.sessions() as db:
            await db.execute(update(AgentProposal).where(AgentProposal.id == pid).values(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
            await db.commit()
        child, stage, status = self.start_child("retention")
        await self.wait_stage(child, stage, "retention_started")
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            if status.exists() and inspect_status(status)[1] == 0:
                async with self.sessions() as db:
                    proposal = await db.get(AgentProposal, pid)
                    if proposal.actions == [] and proposal.status == "expired":
                        break
            await asyncio.sleep(0.05)
        else:
            self.fail("standalone worker did not clear expired original text")
        self.assertNotIn(raw, status.read_text(encoding="utf-8"))
        # Temporarily rename only this schema's table to simulate a database
        # failure. Health must fail without persisting SQL, URL or payloads.
        async with self.engine.begin() as conn:
            await conn.exec_driver_sql("ALTER TABLE agent_proposals RENAME TO unavailable_proposals")
        try:
            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                info, code = inspect_status(status)
                if code == 1 and info.get("consecutive_failures", 0) > 0:
                    self.assertTrue(info["last_error_type"])
                    self.assertNotIn("postgresql", status.read_text(encoding="utf-8"))
                    break
                await asyncio.sleep(0.05)
            else:
                self.fail("worker failed to report database cleanup failure")
        finally:
            async with self.engine.begin() as conn:
                await conn.exec_driver_sql("ALTER TABLE unavailable_proposals RENAME TO agent_proposals")
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            if inspect_status(status)[1] == 0:
                break
            await asyncio.sleep(0.05)
        else:
            self.fail("worker did not recover after the database was restored")

    async def test_full_legacy_e2e_over_real_http_is_isolated(self):
        server, stage, _ = self.start_child("http_server")
        ready = await self.wait_stage(server, stage, "http_server")
        child = subprocess.Popen(
            [sys.executable, str(self.workspace / "test_e2e.py"), f"http://127.0.0.1:{ready['port']}"],
            cwd=self.workspace, env={**os.environ, "PYTHONIOENCODING": "utf-8", "E2E_ISOLATED_DB": "1"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.addAsyncCleanup(self.stop_child, child)
        output, errors = await asyncio.wait_for(asyncio.to_thread(child.communicate), timeout=45)
        self.assertEqual(child.returncode, 0, output.decode("utf-8") + errors.decode("utf-8"))
        self.assertIn("[OK]", output.decode("utf-8"))
        async with self.sessions() as db:
            # The original fixture Trip remains; the E2E-created Trip and its
            # cascading feedback/plans/proposals have been deleted.
            self.assertEqual(await db.scalar(select(func.count()).select_from(Trip)), 1)
            self.assertEqual(await db.scalar(select(func.count()).select_from(AgentProposal)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ItineraryPlan)), 0)
