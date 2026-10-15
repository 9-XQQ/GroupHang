"""Real-process synthetic demo acceptance; no existing users or external services."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from scripts import run_demo


class LocalDemoUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_ttl_and_existing_directory_rejected_before_configuration(self):
        from types import SimpleNamespace
        original = dict(os.environ)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                await run_demo.run(SimpleNamespace(ttl=0, run_dir=Path(directory) / "unused"))
            self.assertFalse((Path(directory) / "unused").exists())
            with self.assertRaises(FileExistsError):
                await run_demo.run(SimpleNamespace(ttl=30, run_dir=Path(directory)))
        self.assertEqual(dict(os.environ), original)

    def test_guide_escapes_names_and_does_not_offer_credentials_or_autologin(self):
        guide = run_demo.demo_guide([{"id": 1, "title": "<script>bad</script>", "invite_code": "DEMO01"}])
        self.assertNotIn("<script>bad</script>", guide)
        self.assertIn("&lt;script&gt;", guide)
        self.assertNotIn("/auth/password/login", guide)
        self.assertIn("估算", guide)


@unittest.skipUnless(os.getenv("AGENT_DB_TESTS") == "1", "set AGENT_DB_TESTS=1 for disposable demo process")
class LocalDemoProcessTests(unittest.IsolatedAsyncioTestCase):
    async def launch(self, ttl=40):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name) / "run"
        child = subprocess.Popen([sys.executable, "scripts/run_demo.py", "--run-dir", str(folder), "--ttl", str(ttl)],
            cwd=run_demo.ROOT, env={**os.environ, "DATABASE_URL": os.getenv("TEST_DATABASE_URL", settings.database_url),
                                   "PYTHONIOENCODING": "utf-8"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        async def cleanup():
            if child.poll() is None:
                (folder / "stop").touch()
                try:
                    await asyncio.to_thread(child.wait, timeout=25)
                except subprocess.TimeoutExpired:
                    child.kill()
                    await asyncio.to_thread(child.wait, timeout=10)
            # If a failing child was forcibly stopped, clean only its recorded random schema.
            state_file = folder / "state.json"
            if state_file.exists():
                state = json.loads(state_file.read_text(encoding="utf-8"))
                schema = state["schema"]
                assert re.fullmatch(r"agent_demo_[0-9a-f]{32}", schema)
                engine = create_async_engine(os.getenv("TEST_DATABASE_URL", settings.database_url), echo=False)
                try:
                    async with engine.begin() as conn:
                        if await conn.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:schema)"), {"schema": schema}):
                            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                finally:
                    await engine.dispose()
        self.addAsyncCleanup(cleanup)
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            try:
                state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
                if state["stage"] == "ready":
                    return child, folder, state
            except (OSError, ValueError):
                pass
            if child.poll() is not None:
                self.fail("Demo exited before ready")
            await asyncio.sleep(.05)
        self.fail("Demo startup timed out")

    async def assert_cleaned(self, child, folder, state):
        await asyncio.to_thread(child.wait, timeout=25)
        self.assertEqual(child.returncode, 0)
        final = json.loads((folder / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(final, {"stage": "cleaned", "schema": state["schema"]})
        self.assertFalse((folder / "credentials.json").exists())
        engine = create_async_engine(os.getenv("TEST_DATABASE_URL", settings.database_url), echo=False)
        try:
            async with engine.connect() as conn:
                self.assertFalse(await conn.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:schema)"), {"schema": state["schema"]}))
        finally:
            await engine.dispose()

    async def test_real_http_scenarios_proposals_traces_conflicts_and_cleanup(self):
        child, folder, state = await self.launch()
        secret = json.loads((folder / "credentials.json").read_text(encoding="utf-8"))
        url = f"http://127.0.0.2:{state['port']}"
        async with httpx.AsyncClient(base_url=url, trust_env=False, timeout=15,
                                     headers={"X-Requested-With": "TripPlanner"}) as client:
            self.assertEqual((await client.get("/ready")).status_code, 200)
            guide = await client.get("/demo")
            self.assertIn("合成演示", guide.text)
            self.assertNotIn(secret["password"], guide.text)
            root = await client.get("/")
            self.assertEqual(root.headers["X-Demo-Fixture"], "synthetic-isolated")
            self.assertIn("模型规则降级", root.text)
            config = (await client.get("/api/config")).json()
            self.assertFalse(config["amap_js_key"])
            self.assertFalse(config["llm_parser_available"])
            login = await client.post("/auth/password/login", json={"username": "demo_1", "password": secret["password"]})
            self.assertEqual(login.status_code, 200)
            owner_token = login.json()["token"]
            client.headers["X-Token"] = owner_token
            trips = state["trips"]
            times = {"start_at": "2026-11-08T05:00:00Z", "end_at": "2026-11-08T10:00:00Z"}
            for index, scene in enumerate(("meal", "brand", "afternoon")):
                request = {"scenario": scene, "city": "上海市", **times}
                if scene == "brand":
                    request["brand"] = "寿司郎"
                if scene == "afternoon":
                    request.update(anchor_name="演示公园入口", return_to_anchor=True)
                response = await client.post(f"/trips/{trips[index]['id']}/assistant/chat", json={"message": "合成演示", "discovery": request})
                self.assertEqual(response.status_code, 200)
                result = response.json()["discovery"]
                self.assertTrue(result["recommendations"])
                self.assertFalse(result["saved"])
                self.assertTrue(all(item["estimated"] for item in result["recommendations"]))
                self.assertNotIn("合成回忆", response.text)

            path = f"/trips/{trips[2]['id']}"
            draft = (await client.post(path + "/assistant/chat", json={"message": "生成当前路线"})).json()
            self.assertFalse(draft["llm_used"])
            self.assertTrue(draft["proposal_id"])
            body = {"proposal_id": draft["proposal_id"]}
            executed = await client.post(path + "/assistant/proposals/execute", json=body)
            self.assertEqual(executed.status_code, 200)
            result = executed.json()
            replay = (await client.post(path + "/assistant/proposals/execute", json=body)).json()
            self.assertTrue(replay["replayed"])
            self.assertNotEqual(result["trace_id"], replay["trace_id"])
            trace_path = path + "/assistant/traces/" + result["trace_id"]
            observed = (await client.get(trace_path)).json()
            self.assertEqual(observed["proposal_id"], draft["proposal_id"])
            self.assertIn("execution.commit", [s["stage"] for s in observed["spans"]])
            plan_id = result["itinerary"]["plan_id"]
            self.assertEqual((await client.post(path + f"/itinerary-plans/{plan_id}/confirm", json={})).status_code, 409)
            self.assertEqual((await client.post(path + f"/itinerary-plans/{plan_id}/confirm", json={"accept_estimated_routes": True})).status_code, 200)
            other = (await client.post("/auth/password/login", json={"username": "demo_2", "password": secret["password"]})).json()
            client.headers["X-Token"] = other["token"]
            self.assertEqual((await client.get(trace_path)).status_code, 404)
            self.assertEqual((await client.get("/users/me/saved-places")).json()["total"], 0)
            client.headers["X-Token"] = owner_token
            conflict = f"/trips/{trips[3]['id']}"
            before = (await client.post(conflict + "/itinerary-plans")).json()
            self.assertTrue(before["warnings"])
            self.assertGreater(before["total_duration_min"], 60)
            destinations = (await client.get(conflict + "/destinations")).json()["destinations"]
            for destination in destinations:
                update = await client.put(conflict + f"/destinations/{destination['id']}", json={"expected_stay_min": 15})
                self.assertEqual(update.status_code, 200)
            after = (await client.post(conflict + "/itinerary-plans")).json()
            self.assertLess(after["total_duration_min"], before["total_duration_min"])
            self.assertLessEqual(after["total_duration_min"], 60)
            comparison = await client.get(conflict + "/itinerary-plans/comparison")
            self.assertEqual(comparison.status_code, 200)
            self.assertTrue(comparison.json()["available"])
        (folder / "stop").touch()
        await self.assert_cleaned(child, folder, state)

    async def test_ttl_automatically_cleans_and_new_run_resets_data(self):
        child, folder, state = await self.launch(ttl=5)
        await self.assert_cleaned(child, folder, state)
        second, second_folder, second_state = await self.launch(ttl=5)
        self.assertNotEqual(state["schema"], second_state["schema"])
        self.assertEqual(state["trips"], second_state["trips"])
        await self.assert_cleaned(second, second_folder, second_state)
