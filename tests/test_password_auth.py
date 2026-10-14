"""Password login acceptance: disposable schema, no SMS or existing accounts."""
import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import func, select, update

from app.config import settings
from app.db import get_db
from app.main import app
from app.models import AuthRateLimit, AuthSession, PasswordCredential, Trip, User
from app.routers import auth
from app.schemas import PasswordRegisterRequest
from app.services.password_auth import hash_password, verify_password, reserve_auth_attempt
from app.services.auth_retention import cleanup_auth_data
from scripts.set_account_password import set_account_password
from tests import test_agent_execution as fixtures

PASSWORD = "Test-pass2026!"
NEW_PASSWORD = "New-pass2026!"


class PasswordHashTests(unittest.TestCase):
    def test_random_salts_unicode_and_exact_password_preservation(self):
        value = "  空格与大小写 Test 密码长句  "
        first, second = hash_password(value), hash_password(value)
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password(value, first))
        self.assertFalse(verify_password(value.strip(), first))
        self.assertFalse(verify_password(value.lower(), first))
        self.assertFalse(verify_password(value, "invalid"))
        self.assertFalse(verify_password(value, first.replace("32768", "2147483648")))

    def test_schema_normalizes_only_username(self):
        body = PasswordRegisterRequest(username="  Alice_01 ", password="  Pass Test  ")
        self.assertEqual(body.username, "alice_01")
        self.assertEqual(body.password, "  Pass Test  ")
        self.assertNotIn(body.password, repr(body))


    def test_new_password_length_boundaries_and_legacy_login(self):
        from pydantic import ValidationError
        from app.schemas import PasswordChangeRequest, PasswordLoginRequest
        for length in (8, 15):
            PasswordRegisterRequest(username="alice", password="a" * length)
            PasswordChangeRequest(current_password="old", new_password="a" * length)
        for length in (7, 16):
            with self.assertRaises(ValidationError):
                PasswordRegisterRequest(username="alice", password="a" * length)
            with self.assertRaises(ValidationError):
                PasswordChangeRequest(current_password="old", new_password="a" * length)
        PasswordLoginRequest(username="alice", password="legacy long password 2026")
        PasswordChangeRequest(current_password="legacy long password 2026", new_password="Newpass8")


@unittest.skipUnless(os.getenv("AGENT_DB_TESTS") == "1", "set AGENT_DB_TESTS=1")
class PasswordAuthDatabaseTests(unittest.IsolatedAsyncioTestCase):
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
        config = patch.multiple(settings, sms_provider="disabled", auth_secret="",
                                app_env="development", auth_cookie_secure=False, cors_origins=[])
        config.start()
        self.addCleanup(config.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                        headers={"X-Requested-With": "TripPlanner"})
        self.addAsyncCleanup(self.client.aclose)

    async def register(self, username="alice", browser=False):
        return await self.client.post("/auth/register", json={
            "username": username, "password": PASSWORD, "browser": browser, "name": "Alice",
        })

    async def login(self, username="alice", password=PASSWORD, browser=False):
        return await self.client.post("/auth/password/login", json={
            "username": username, "password": password, "browser": browser,
        })

    async def test_register_and_login_work_with_sms_and_secret_unconfigured(self):
        first = await self.register()
        self.assertEqual(first.status_code, 200, first.text)
        uid = first.json()["user"]["id"]
        self.assertIsNone(first.json()["user"]["phone"])
        logged = await self.login("ALICE")
        self.assertEqual(logged.status_code, 200, logged.text)
        self.assertEqual(logged.json()["user"]["id"], uid)
        async with self.sessions() as db:
            credential = await db.get(PasswordCredential, uid)
            self.assertNotIn(PASSWORD, credential.password_hash)
            self.assertTrue(verify_password(PASSWORD, credential.password_hash))

    async def test_cookie_login_restore_logout_and_data_access(self):
        registered = await self.register(browser=True)
        self.assertNotIn("token", registered.json())
        self.assertIn("HttpOnly", registered.headers["set-cookie"])
        self.assertEqual((await self.client.get("/auth/me")).status_code, 200)
        trip = await self.client.post("/trips", json={"title": "password trip"})
        self.assertEqual(trip.status_code, 201, trip.text)
        await self.client.post("/auth/logout")
        self.assertEqual((await self.client.get("/trips")).status_code, 401)
        self.assertEqual((await self.login(browser=True)).status_code, 200)
        listed = await self.client.get("/trips")
        self.assertEqual(listed.json()["trips"][0]["trip_id"], trip.json()["trip_id"])

    async def test_wrong_password_and_unknown_username_have_same_error(self):
        await self.register(browser=True)
        wrong = await self.login(password="wrong")
        unknown = await self.login(username="unknown", password="wrong")
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(wrong.json(), unknown.json())
        self.assertEqual((await self.client.get("/auth/me")).status_code, 200)

    async def test_duplicate_username_is_case_insensitive_and_atomic(self):
        responses = await asyncio.gather(self.register("ALICE"), self.register("alice"))
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(PasswordCredential)), 1)
            self.assertEqual(await db.scalar(select(func.count()).select_from(User)), 3)

    async def test_invalid_password_not_echoed_and_phone_cannot_claim_existing_account(self):
        for body in (
            {"username": "alice", "password": "short"},
            {"username": "bad-name", "password": PASSWORD},
            {"username": "alice", "password": PASSWORD, "phone": "13900000001"},
        ):
            response = await self.client.post("/auth/register", json=body)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(body["password"], response.text)

    async def test_login_budget_survives_errors_and_blocks_before_hashing(self):
        await self.register()
        for _ in range(10):
            self.assertEqual((await self.login(password="wrong")).status_code, 401)
        with patch.object(auth, "password_work") as work:
            limited = await self.login()
            self.assertEqual(limited.status_code, 429)
            work.assert_not_called()
        async with self.sessions() as db:
            await db.execute(update(AuthRateLimit).values(
                window_started_at=datetime.now(timezone.utc) - timedelta(hours=1)))
            await db.commit()
        self.assertEqual((await self.login()).status_code, 200)

    async def test_database_budget_is_shared_across_concurrent_sessions(self):
        async def attempt(i):
            async with self.sessions() as db:
                try:
                    await reserve_auth_attempt(db, "login", "shared", str(i))
                    return 200
                except Exception as exc:
                    return exc.status_code
        results = await asyncio.gather(*[attempt(i) for i in range(12)])
        self.assertEqual(results.count(200), 10)
        self.assertEqual(results.count(429), 2)

    async def test_registration_ip_limit_and_origin_csrf_checks(self):
        for i in range(5):
            self.assertEqual((await self.register("user" + str(i))).status_code, 200)
        self.assertEqual((await self.register("user6")).status_code, 429)
        response = await self.client.post("/auth/register", json={
            "username": "user7", "password": PASSWORD}, headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        logged = await self.login("user0", browser=True)
        self.assertEqual(logged.status_code, 200)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     cookies=self.client.cookies) as client:
            response = await client.post("/auth/password/change", json={
                "current_password": PASSWORD, "new_password": NEW_PASSWORD})
            self.assertEqual(response.status_code, 403)

    async def test_change_password_revokes_all_old_sessions_and_issues_new_cookie(self):
        registered = await self.register()
        token = registered.json()["token"]
        await self.login(browser=True)
        old_cookie = self.client.cookies.get("trip_session")
        changed = await self.client.post("/auth/password/change", json={
            "current_password": PASSWORD, "new_password": NEW_PASSWORD, "browser": True})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual((await self.client.get("/auth/me")).status_code, 200)
        for old in (token, old_cookie):
            self.assertEqual((await self.client.get("/auth/me", headers={"X-Token": old})).status_code, 401)
        self.assertEqual((await self.login(password=PASSWORD)).status_code, 401)
        self.assertEqual((await self.login(password=NEW_PASSWORD)).status_code, 200)

    async def test_wrong_current_password_preserves_session(self):
        await self.register(browser=True)
        changed = await self.client.post("/auth/password/change", json={
            "current_password": "wrong", "new_password": NEW_PASSWORD, "browser": True})
        self.assertEqual(changed.status_code, 400)
        self.assertEqual((await self.client.get("/auth/me")).status_code, 200)

    async def test_registration_transaction_failure_keeps_no_account_or_credential(self):
        with patch.object(auth, "complete_login", AsyncMock(side_effect=RuntimeError("injected"))):
            with self.assertRaises(RuntimeError):
                await self.register()
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(User)), 2)
            self.assertEqual(await db.scalar(select(func.count()).select_from(PasswordCredential)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(AuthRateLimit)), 1)

    async def test_admin_binding_preserves_original_user_trip_and_preferences(self):
        from app.security import issue_token
        async with self.sessions() as db:
            original = await db.get(User, self.user.id)
            original.preferences = {"preserve": True}
            token, _ = await issue_token(db, self.user.id)
            await db.commit()
            await set_account_password(db, self.user.id, "existing_user", hash_password(PASSWORD))
            await db.commit()
        logged = await self.login("existing_user")
        self.assertEqual(logged.status_code, 200, logged.text)
        self.assertEqual(logged.json()["user"]["id"], self.user.id)
        self.assertEqual((await self.client.get("/auth/me", headers={"X-Token": token})).status_code, 401)
        async with self.sessions() as db:
            self.assertEqual((await db.get(User, self.user.id)).preferences, {"preserve": True})
            self.assertEqual((await db.get(Trip, self.trip.id)).creator_id, self.user.id)
            with self.assertRaises(ValueError):
                await set_account_password(db, self.other.id, "existing_user", hash_password(PASSWORD))
            await db.rollback()

    async def test_reset_during_login_hash_does_not_issue_session_with_old_password(self):
        registered = await self.register()
        uid = registered.json()["user"]["id"]
        async def race(function, *args):
            valid = function(*args)
            async with self.sessions() as db:
                await set_account_password(db, uid, "alice", hash_password(NEW_PASSWORD))
                await db.commit()
            return valid
        with patch.object(auth, "password_work", race):
            self.assertEqual((await self.login()).status_code, 401)
        self.assertEqual((await self.login(password=NEW_PASSWORD)).status_code, 200)

    async def test_profile_is_private_and_updates_only_own_nickname(self):
        self.assertEqual((await self.client.get("/users/me/profile")).status_code, 401)
        self.assertEqual((await self.client.patch("/users/me/profile", json={"name": "游客"})).status_code, 401)
        registered = await self.register(browser=True)
        uid = registered.json()["user"]["id"]
        profile = await self.client.get("/users/me/profile")
        self.assertEqual(profile.json(), {"name": "Alice", "username": "alice"})
        updated = await self.client.patch("/users/me/profile", json={"name": "  鱼头  "})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json(), {"name": "鱼头"})
        self.assertEqual((await self.client.get("/auth/me")).json()["user"]["name"], "鱼头")
        for body in ({"name": "   "}, {"name": "x" * 51}, {"name": "入侵", "user_id": self.other.id},
                     {"name": "入侵", "username": "other"}):
            self.assertEqual((await self.client.patch("/users/me/profile", json=body)).status_code, 422)
        async with self.sessions() as db:
            self.assertEqual((await db.get(User, uid)).name, "鱼头")
            self.assertNotEqual((await db.get(User, self.other.id)).name, "鱼头")
        saved = await self.client.put("/users/me/preferences", json={
            "personalization_enabled": True, "explicit": {"preferred_categories": ["公园"]}})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual((await self.client.get("/users/me/preferences")).json()["explicit"]["preferred_categories"], ["公园"])

    async def test_existing_long_password_can_login_and_change_to_shorter_password(self):
        await self.register()
        legacy = "legacy long password before change"
        async with self.sessions() as db:
            uid = await db.scalar(select(PasswordCredential.user_id).where(PasswordCredential.username == "alice"))
            await set_account_password(db, uid, "alice", hash_password(legacy))
            await db.commit()
        self.assertEqual((await self.login(password=legacy, browser=True)).status_code, 200)
        changed = await self.client.post("/auth/password/change", json={
            "current_password": legacy, "new_password": "Newpass8", "browser": True})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual((await self.login(password="Newpass8")).status_code, 200)

    async def test_revoke_others_preserves_current_and_other_accounts(self):
        registered = await self.register()
        other_token = registered.json()["token"]
        uid = registered.json()["user"]["id"]
        await self.login(browser=True)
        async with self.sessions() as db:
            from app.security import issue_token
            unrelated, _ = await issue_token(db, self.other.id)
            expired, _ = await issue_token(db, uid)
            await db.flush()
            from app.security import token_digest
            expired_session = await db.get(AuthSession, token_digest(expired))
            expired_session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()
        before = await self.client.get("/users/me/sessions")
        self.assertEqual(before.json(), {"active_sessions": 2})
        self.assertEqual(before.headers["cache-control"], "no-store")
        self.assertNotIn(other_token, before.text)
        revoked = await self.client.post("/users/me/sessions/revoke-others", json={})
        self.assertEqual(revoked.json(), {"revoked_sessions": 1})
        self.assertEqual((await self.client.get("/auth/me")).status_code, 200)
        self.assertEqual((await self.client.get("/auth/me", headers={"X-Token": other_token})).status_code, 401)
        self.assertEqual((await self.client.get("/auth/me", headers={"X-Token": unrelated})).status_code, 200)
        self.assertEqual((await self.client.get("/users/me/sessions")).json(), {"active_sessions": 1})
        self.assertEqual((await self.client.post("/users/me/sessions/revoke-others", json={})).json(),
                         {"revoked_sessions": 0})

    async def test_session_management_requires_auth_and_cookie_csrf_header(self):
        self.assertEqual((await self.client.get("/users/me/sessions")).status_code, 401)
        self.assertEqual((await self.client.post("/users/me/sessions/revoke-others", json={})).status_code, 401)
        await self.register(browser=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     cookies=self.client.cookies) as client:
            self.assertEqual((await client.post("/users/me/sessions/revoke-others", json={})).status_code, 403)
        self.assertEqual((await self.client.post("/users/me/sessions/revoke-others", json={},
                         headers={"Origin": "https://evil.example"})).status_code, 403)
        self.assertEqual((await self.client.get("/auth/me")).status_code, 200)

    async def test_rate_metadata_cleanup(self):
        await self.register()
        async with self.sessions() as db:
            await db.execute(update(AuthRateLimit).values(
                window_started_at=datetime.now(timezone.utc) - timedelta(days=2)))
            await cleanup_auth_data(db)
            await db.commit()
            self.assertEqual(await db.scalar(select(func.count()).select_from(AuthRateLimit)), 0)
