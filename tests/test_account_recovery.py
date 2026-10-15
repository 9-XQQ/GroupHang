"""Offline recovery is opt-in, single-use and serialized with password resets."""
import asyncio
import hashlib
import os
import unittest

import httpx
from sqlalchemy import select
from app.models import AccountRecoveryCode, PasswordCredential
from app.schemas import PasswordRecoverRequest
from app.services.password_auth import hash_password
from scripts.set_account_password import set_account_password
from tests import test_password_auth as password_tests
from app.main import app
PASSWORD = password_tests.PASSWORD
NEW_PASSWORD = password_tests.NEW_PASSWORD


class RecoverySchemaTests(unittest.TestCase):
    def test_recovery_secret_normalization_and_no_repr(self):
        code = "ABCD1234-" * 5 + "ABCD1234"
        body = PasswordRecoverRequest(username=" ALICE ", recovery_code=code, new_password=NEW_PASSWORD)
        self.assertEqual(body.username, "alice")
        self.assertEqual(body.recovery_code, "abcd1234" * 6)
        self.assertNotIn(body.recovery_code, repr(body))
        self.assertNotIn(NEW_PASSWORD, repr(body))


@unittest.skipUnless(os.getenv("AGENT_DB_TESTS") == "1", "set AGENT_DB_TESTS=1")
class AccountRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = password_tests.PasswordAuthDatabaseTests.asyncSetUp
    cleanup_schema = password_tests.PasswordAuthDatabaseTests.cleanup_schema
    register = password_tests.PasswordAuthDatabaseTests.register
    login = password_tests.PasswordAuthDatabaseTests.login

    async def setup_code(self, browser=False):
        response = await self.register(browser=browser)
        self.assertEqual(response.status_code, 200)
        self.uid = response.json()["user"]["id"]
        self.headers = {} if browser else {"X-Token": response.json()["token"]}
        created = await self.client.post("/auth/recovery-code", json={"current_password": PASSWORD}, headers=self.headers)
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.headers["cache-control"], "no-store")
        return created.json()["recovery_code"]

    async def recover(self, code, username="alice", password=NEW_PASSWORD):
        return await self.client.post("/auth/password/recover", json={
            "username": username, "recovery_code": code, "new_password": password,
        })

    async def test_code_is_hash_only_rotates_and_status_never_discloses_secret(self):
        first = await self.setup_code()
        status = await self.client.get("/auth/recovery-code", headers=self.headers)
        self.assertEqual(status.json(), {"enabled": True})
        async with self.sessions() as db:
            row = await db.get(AccountRecoveryCode, self.uid)
            self.assertEqual(row.code_hash, hashlib.sha256(first.replace("-", "").encode()).hexdigest())
        second = await self.client.post("/auth/recovery-code", json={"current_password": PASSWORD}, headers=self.headers)
        self.assertNotEqual(first, second.json()["recovery_code"])
        self.assertEqual((await self.recover(first)).status_code, 400)
        self.assertEqual((await self.recover(second.json()["recovery_code"])).status_code, 200)

    async def test_recovery_once_revokes_all_sessions_preserves_data_and_requires_login(self):
        code = await self.setup_code()
        other_login = await self.login()
        other_token = other_login.json()["token"]
        trip = await self.client.post("/trips", json={"title": "保留行程"}, headers=self.headers)
        self.assertEqual(trip.status_code, 201)
        result = await self.recover(code)
        self.assertEqual(result.status_code, 200)
        self.assertNotIn("token", result.json())
        self.assertEqual(result.headers["cache-control"], "no-store")
        for headers in (self.headers, {"X-Token": other_token}):
            self.assertEqual((await self.client.get("/auth/me", headers=headers)).status_code, 401)
        self.assertEqual((await self.login()).status_code, 401)
        new_login = await self.login(password=NEW_PASSWORD)
        self.assertEqual(new_login.status_code, 200)
        self.assertEqual(new_login.json()["user"]["id"], self.uid)
        trips = await self.client.get("/trips", headers={"X-Token": new_login.json()["token"]})
        self.assertEqual(trips.json()["trips"][0]["title"], "保留行程")
        self.assertEqual((await self.recover(code)).status_code, 400)

    async def test_concurrent_consumption_succeeds_exactly_once(self):
        code = await self.setup_code()
        outcomes = await asyncio.gather(self.recover(code), self.recover(code))
        self.assertEqual(sorted(r.status_code for r in outcomes), [200, 400])

    async def test_invalid_and_unknown_account_errors_match_without_consuming_code(self):
        code = await self.setup_code()
        wrong = await self.recover("0" * 48)
        unknown = await self.recover(code, username="unknown")
        self.assertEqual(wrong.json(), unknown.json())
        self.assertEqual((await self.recover(code)).status_code, 200)

    async def test_change_and_admin_reset_invalidate_recovery_code(self):
        code = await self.setup_code()
        changed = await self.client.post("/auth/password/change", json={"current_password": PASSWORD,
             "new_password": NEW_PASSWORD}, headers=self.headers)
        self.assertEqual(changed.status_code, 200)
        self.assertEqual((await self.recover(code)).status_code, 400)
        headers = {"X-Token": changed.json()["token"]}
        new_code = await self.client.post("/auth/recovery-code", json={"current_password": NEW_PASSWORD}, headers=headers)
        self.assertEqual(new_code.status_code, 200)
        async with self.sessions() as db:
            await set_account_password(db, self.uid, "alice", hash_password(PASSWORD))
            await db.commit()
        self.assertEqual((await self.recover(new_code.json()["recovery_code"])).status_code, 400)

    async def test_auth_origin_validation_format_and_rate_limit(self):
        code = await self.setup_code(browser=True)
        self.assertEqual((await self.client.post("/auth/recovery-code", json={"current_password": "wrong"})).status_code, 400)
        self.assertEqual((await self.client.post("/auth/recovery-code", json={"current_password": PASSWORD},
            headers={"Origin": "https://untrusted.example"})).status_code, 403)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test", cookies=self.client.cookies) as unsafe:
            self.assertEqual((await unsafe.post("/auth/recovery-code", json={"current_password": PASSWORD})).status_code, 403)
        bad = await self.client.post("/auth/password/recover", json={"username":"alice", "recovery_code":code,
                                                                     "new_password":"short"})
        self.assertEqual(bad.status_code, 422)
        self.assertNotIn(code, bad.text)
        for _ in range(10):
            self.assertEqual((await self.recover("0" * 48)).status_code, 400)
        self.assertEqual((await self.recover("0" * 48)).status_code, 429)
