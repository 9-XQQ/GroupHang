"""Password hashing and database-backed authentication request budgets."""
import asyncio
import base64
import hashlib
import hmac
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import case, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuthRateLimit

# OWASP scrypt alternative: 32 MiB, r=8, p=3. Never use plain SHA for passwords.
PARAMS = "scrypt$32768$8$3"
DUMMY_HASH = PARAMS + "$" + base64.b64encode(b"\x00" * 16).decode() + "$" + base64.b64encode(b"\x00" * 64).decode()
_HASH_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="password-hash")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    value = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=3,
                           maxmem=64 * 1024 * 1024, dklen=64)
    return PARAMS + "$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(value).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        prefix, n, r, p, salt_text, hash_text = encoded.split("$")
        if "$".join((prefix, n, r, p)) != PARAMS:
            return False
        salt = base64.b64decode(salt_text, validate=True)
        expected = base64.b64decode(hash_text, validate=True)
        if len(salt) != 16 or len(expected) != 64:
            return False
        actual = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=3,
                                maxmem=64 * 1024 * 1024, dklen=64)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


async def password_work(function, *args):
    # Bound memory-heavy hashing across requests; don't block the ASGI loop.
    return await asyncio.get_running_loop().run_in_executor(_HASH_POOL, function, *args)


async def reserve_auth_attempt(db: AsyncSession, scope: str, username: str, ip: str) -> None:
    if scope == "register":
        budgets = [(f"register-ip:{ip}", 5, 3600)]
    else:
        budgets = [(f"{scope}-account:{username}", 10, 900),
                   (f"{scope}-ip:{ip}", 30, 900)]
    exceeded = False
    retry = 0
    # Atomic upserts serialize workers; every attempt is reserved before hashing.
    for value, limit, seconds in sorted(budgets):
        key = hashlib.sha256(value.encode()).hexdigest()
        clock = func.clock_timestamp()
        reset = AuthRateLimit.window_started_at <= clock - timedelta(seconds=seconds)
        statement = insert(AuthRateLimit).values(key=key, attempts=1, window_started_at=clock)
        statement = statement.on_conflict_do_update(
            index_elements=[AuthRateLimit.key],
            set_={"attempts": case((reset, 1), else_=func.least(AuthRateLimit.attempts + 1, limit + 1)),
                  "window_started_at": case((reset, clock), else_=AuthRateLimit.window_started_at)},
        ).returning(AuthRateLimit.attempts)
        if (await db.scalar(statement)) > limit:
            exceeded = True
            retry = max(retry, seconds)
    await db.commit()
    if exceeded:
        raise HTTPException(429, "尝试次数过多，请稍后再试", headers={"Retry-After": str(retry)})
