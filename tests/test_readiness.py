"""Operational probes must fail closed without exposing database errors."""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.main import app


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, path):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await client.get(path)

    def connection(self):
        connection = MagicMock()
        connection.execute = AsyncMock()
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=connection)
        context.__aexit__ = AsyncMock(return_value=False)
        engine = MagicMock()
        engine.connect.return_value = context
        return engine, connection, context

    async def test_liveness_does_not_connect_to_database(self):
        with patch("app.main.engine") as engine:
            response = await self.request("/health")
        self.assertEqual(response.status_code, 200)
        engine.connect.assert_not_called()

    async def test_ready_after_database_query_and_connection_release(self):
        engine, connection, context = self.connection()
        with patch("app.main.engine", engine):
            response = await self.request("/ready")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ready"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(str(connection.execute.await_args.args[0]), "SELECT 1")
        context.__aexit__.assert_awaited_once()

    async def test_database_failure_returns_generic_503_even_in_debug(self):
        engine, connection, context = self.connection()
        connection.execute.side_effect = RuntimeError("secret database credential")
        with patch("app.main.engine", engine):
            response = await self.request("/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "not_ready"})
        self.assertNotIn("secret", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        context.__aexit__.assert_awaited_once()

    async def test_slow_database_is_cancelled_and_connection_released(self):
        engine, connection, context = self.connection()
        async def wait_forever(*args):
            await asyncio.Event().wait()
        connection.execute.side_effect = wait_forever
        with patch("app.main.engine", engine), patch("app.main.READINESS_TIMEOUT_SECONDS", 0.02):
            response = await self.request("/ready")
        self.assertEqual(response.status_code, 503)
        context.__aexit__.assert_awaited_once()
