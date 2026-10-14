"""Phone OTP/session tests: disposable PostgreSQL schema, never real SMS."""
import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from pydantic import ValidationError
from sqlalchemy import func, select, update

from app.config import Settings, settings
from app.db import get_db
from app.main import app
from app.models import AuthSession, SmsChallenge, User
from app.security import COOKIE_NAME, token_digest
from app.services import phone_auth, sms
from app.services.auth_retention import cleanup_auth_data
from tests import test_agent_execution as fixtures


class PhoneAuthConfigTests(unittest.TestCase):
    def test_production_rejects_mock_debug_and_insecure_cookie(self):
        safe = dict(_env_file=None, app_env="production", debug=False,
                    auth_cookie_secure=True, auth_secret="x" * 40)
        Settings(**safe)
        for bad in ({"sms_provider": "mock"}, {"debug": True},
                    {"auth_cookie_secure": False}, {"auth_secret": ""}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                Settings(**{**safe, **bad})

    def test_unknown_environment_and_wildcard_cors_fail_closed(self):
        for bad in ({"app_env": "prod"}, {"cors_origins": ["*"]},
                    {"app_env": "test", "sms_provider": "mock"}):
            with self.assertRaises(ValidationError):
                Settings(_env_file=None, **bad)


@unittest.skipUnless(os.getenv("AGENT_DB_TESTS") == "1", "set AGENT_DB_TESTS=1")
class PhoneAuthDatabaseTests(unittest.IsolatedAsyncioTestCase):
    cleanup_schema = fixtures.AgentExecutionDatabaseTests.cleanup_schema

    async def asyncSetUp(self):
        await fixtures.AgentExecutionDatabaseTests.asyncSetUp(self)
        self.previous = dict(app.dependency_overrides)
        async def isolated_db():
            async with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = isolated_db
        self.addCleanup(self.restore_overrides)
        self.config = patch.multiple(settings, auth_secret="test-only-secret-" + "x" * 32,
                                     sms_provider="mock", app_env="development",
                                     auth_cookie_secure=False, cors_origins=[])
        self.config.start()
        self.addCleanup(self.config.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                       base_url="http://test",
                                       headers={"X-Requested-With": "TripPlanner"})
        self.addAsyncCleanup(self.client.aclose)
        self.phone = "13900000001"

    def restore_overrides(self):
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.previous)

    async def code(self, phone=None, client=None):
        response = await (client or self.client).post("/auth/sms-code", json={"phone": phone or self.phone})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["dev_code"]

    async def login(self, code, browser=False, phone=None):
        return await self.client.post("/auth/login", json={
            "phone": phone or self.phone, "code": code, "browser": browser,
        })

    async def age_challenges(self):
        async with self.sessions() as db:
            await db.execute(update(SmsChallenge).values(
                created_at=datetime.now(timezone.utc) - timedelta(minutes=2)))
            await db.commit()

    async def test_no_code_and_wechat_placeholder_cannot_login(self):
        for body in ({"phone": self.phone}, {"wechat_code": "arbitrary"},
                     {"phone": self.phone, "code": "12345"}):
            response = await self.client.post("/auth/login", json=body)
            self.assertIn(response.status_code, (400, 422))
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(AuthSession)), 0)

    async def test_code_single_use_and_only_hashes_are_stored(self):
        code = await self.code()
        logged = await self.login(code)
        self.assertEqual(logged.status_code, 200, logged.text)
        token = logged.json()["token"]
        self.assertEqual((await self.login(code)).status_code, 400)
        async with self.sessions() as db:
            row = (await db.execute(select(SmsChallenge))).scalar_one()
            self.assertEqual(row.code_hash, "")
            self.assertEqual(row.status, "consumed")
            self.assertNotEqual(row.phone_hash, self.phone)
            session = (await db.execute(select(AuthSession))).scalar_one()
            self.assertEqual(session.token_hash, token_digest(token))
            self.assertNotEqual(session.token_hash, token)

    async def test_wrong_attempts_persist_and_lock_even_correct_code(self):
        code = await self.code()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            self.assertEqual((await self.login(wrong)).status_code, 400)
        self.assertEqual((await self.login(code)).status_code, 400)
        async with self.sessions() as db:
            row = (await db.execute(select(SmsChallenge))).scalar_one()
            self.assertEqual(row.attempts, 5)
            self.assertEqual(row.code_hash, "")

    async def test_expired_and_superseded_codes_are_rejected(self):
        old = await self.code()
        await self.age_challenges()
        with patch.object(phone_auth.secrets, "randbelow", return_value=(int(old) + 1) % 1000000):
            new = await self.code()
        self.assertEqual((await self.login(old)).status_code, 400)
        async with self.sessions() as db:
            await db.execute(update(SmsChallenge).values(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
            await db.commit()
        self.assertEqual((await self.login(new)).status_code, 400)

    async def test_concurrent_send_and_verify_are_serialized(self):
        send = await asyncio.gather(*[
            self.client.post("/auth/sms-code", json={"phone": self.phone}) for _ in range(2)
        ])
        self.assertEqual(sorted(r.status_code for r in send), [200, 429])
        code = next(r.json()["dev_code"] for r in send if r.status_code == 200)
        verify = await asyncio.gather(self.login(code), self.login(code))
        self.assertEqual(sorted(r.status_code for r in verify), [200, 400])
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(AuthSession)), 1)

    async def test_phone_daily_and_ip_hourly_limits(self):
        now = datetime.now(timezone.utc)
        async with self.sessions() as db:
            for i in range(10):
                db.add(SmsChallenge(id=uuid4().hex, phone_hash=phone_auth.digest("phone:" + self.phone),
                                    ip_hash="other", code_hash="", status="failed", attempts=0,
                                    created_at=now - timedelta(minutes=2), expires_at=now))
            await db.commit()
        response = await self.client.post("/auth/sms-code", json={"phone": self.phone})
        self.assertEqual(response.status_code, 429)
        async with self.sessions() as db:
            for i in range(30):
                db.add(SmsChallenge(id=uuid4().hex, phone_hash=str(i),
                                    ip_hash=phone_auth.digest("ip:127.0.0.1"), code_hash="",
                                    status="failed", attempts=0,
                                    created_at=now - timedelta(minutes=2), expires_at=now))
            await db.commit()
        response = await self.client.post("/auth/sms-code", json={"phone": "13900000002"})
        self.assertEqual(response.status_code, 429)

    async def test_failed_delivery_is_not_usable_and_consumes_quota(self):
        with patch.object(sms, "send_code", AsyncMock(side_effect=RuntimeError("private provider error"))):
            response = await self.client.post("/auth/sms-code", json={"phone": self.phone})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)
        async with self.sessions() as db:
            row = (await db.execute(select(SmsChallenge))).scalar_one()
            self.assertEqual(row.status, "failed")
            self.assertEqual(row.code_hash, "")
        self.assertEqual((await self.client.post("/auth/sms-code", json={"phone": self.phone})).status_code, 429)

    async def test_sending_reservation_cannot_be_verified(self):
        started, resume = asyncio.Event(), asyncio.Event()
        captured = []
        async def delayed(phone, code):
            captured.append(code)
            started.set()
            await resume.wait()
        with patch.object(sms, "send_code", delayed):
            sending = asyncio.create_task(self.client.post("/auth/sms-code", json={"phone": self.phone}))
            try:
                await asyncio.wait_for(started.wait(), 5)
                self.assertEqual((await self.login(captured[0])).status_code, 400)
            finally:
                resume.set()
                await sending

    async def test_cookie_has_security_flags_and_logout_revokes_only_current_device(self):
        code = await self.code()
        api_login = await self.login(code)
        api_token = api_login.json()["token"]
        await self.age_challenges()
        browser = await self.login(await self.code(), browser=True)
        self.assertEqual(browser.status_code, 200, browser.text)
        self.assertNotIn("token", browser.json())
        cookie = browser.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertEqual((await self.client.get("/auth/me")).status_code, 200)
        cookie_token = self.client.cookies.get(COOKIE_NAME)
        logout = await self.client.post("/auth/logout")
        self.assertEqual(logout.status_code, 200)
        self.assertEqual((await self.client.get("/auth/me")).status_code, 401)
        self.assertEqual((await self.client.get("/auth/me", headers={"X-Token": cookie_token})).status_code, 401)
        self.assertEqual((await self.client.get("/auth/me", headers={"X-Token": api_token})).status_code, 200)

    async def test_session_expiry_and_legacy_file_token_fail(self):
        logged = await self.login(await self.code())
        token = logged.json()["token"]
        async with self.sessions() as db:
            await db.execute(update(AuthSession).values(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
            await db.commit()
        for value in (token, "old-file-token"):
            self.assertEqual((await self.client.get("/auth/me", headers={"X-Token": value})).status_code, 401)

    async def test_cookie_mutations_require_header_and_trusted_origin(self):
        await self.login(await self.code(), browser=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     cookies=self.client.cookies) as client:
            self.assertEqual((await client.post("/auth/logout")).status_code, 403)
            self.assertEqual((await client.post("/auth/logout", headers={
                "X-Requested-With": "TripPlanner", "Origin": "https://evil.example"})).status_code, 403)
            self.assertEqual((await client.get("/auth/me")).status_code, 200)
        self.assertEqual((await self.client.post("/auth/sms-code",
            json={"phone": "13900000002"}, headers={"Origin": "https://evil.example"})).status_code, 403)

    async def test_existing_account_survives_and_browser_switch_revokes_previous_session(self):
        first = await self.login(await self.code(), browser=True)
        uid = first.json()["user"]["id"]
        old = self.client.cookies.get(COOKIE_NAME)
        await self.age_challenges()
        second = await self.login(await self.code(), browser=True)
        self.assertEqual(second.json()["user"]["id"], uid)
        self.assertEqual((await self.client.get("/auth/me", headers={"X-Token": old})).status_code, 401)
        self.assertEqual((await self.client.get("/auth/me")).status_code, 200)

    async def test_disabled_provider_and_missing_secret_fail_closed(self):
        for overrides in ({"sms_provider": "disabled"}, {"auth_secret": ""}):
            with patch.multiple(settings, **overrides):
                self.assertEqual((await self.client.post("/auth/sms-code",
                    json={"phone": self.phone})).status_code, 503)

    async def test_cleanup_scrubs_expired_code_keeps_quota_then_removes_metadata(self):
        await self.code()
        async with self.sessions() as db:
            await db.execute(update(SmsChallenge).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
            await db.commit()
            await cleanup_auth_data(db)
            await db.commit()
            row = (await db.execute(select(SmsChallenge))).scalar_one()
            self.assertEqual(row.code_hash, "")
            await db.execute(update(SmsChallenge).values(created_at=datetime.now(timezone.utc) - timedelta(days=3)))
            await cleanup_auth_data(db)
            await db.commit()
            self.assertEqual(await db.scalar(select(func.count()).select_from(SmsChallenge)), 0)


    async def test_session_creation_failure_rolls_back_code_consumption(self):
        from app.routers import auth
        code = await self.code()
        with patch.object(auth, "issue_token", AsyncMock(side_effect=RuntimeError("injected"))):
            with self.assertRaises(RuntimeError):
                await self.login(code)
        self.assertEqual((await self.login(code)).status_code, 200)

    async def test_websocket_membership_checks_expiry_and_revocation(self):
        from app.routers import ws
        from app.security import issue_token
        async with self.sessions() as db:
            token, _ = await issue_token(db, self.user.id)
            await db.commit()
        with patch.object(ws, "SessionLocal", self.sessions):
            self.assertTrue(await ws.socket_member(token, self.trip.id))
            self.assertFalse(await ws.socket_member(token, self.trip.id + 100))
            self.assertFalse(await ws.socket_member("legacy-file-token", self.trip.id))
            async with self.sessions() as db:
                await db.execute(update(AuthSession).values(revoked_at=datetime.now(timezone.utc)))
                await db.commit()
            self.assertFalse(await ws.socket_member(token, self.trip.id))

    async def test_mock_cannot_be_used_from_remote_peer(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("203.0.113.1", 123)),
                                     base_url="http://test") as client:
            response = await client.post("/auth/sms-code", json={"phone": self.phone})
            self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
