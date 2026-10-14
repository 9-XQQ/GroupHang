"""One-use OTPs with database-wide phone/IP throttling."""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import SmsChallenge
from . import sms

OTP_TTL = timedelta(minutes=5)
COOLDOWN = 60
MAX_ATTEMPTS = 5


def digest(value: str) -> str:
    if len(settings.auth_secret) < 32:
        raise HTTPException(503, "登录服务尚未配置")
    return hmac.new(settings.auth_secret.encode(), value.encode(), hashlib.sha256).hexdigest()


async def lock_keys(db: AsyncSession, *keys: str) -> None:
    # Stable signed 64-bit keys shared by all processes; sorted to avoid deadlock.
    for value in sorted(set(keys)):
        key = int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big", signed=True)
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def request_code(db: AsyncSession, phone: str, ip: str) -> dict:
    try:
        sms.ensure_available()
    except sms.SmsUnavailable:
        raise HTTPException(503, "短信服务尚未启用或配置不完整") from None
    phone_key, ip_key = digest("phone:" + phone), digest("ip:" + ip)
    await lock_keys(db, phone_key, ip_key)
    now = datetime.now(timezone.utc)
    latest = (await db.execute(select(SmsChallenge).where(
        SmsChallenge.phone_hash == phone_key,
    ).order_by(SmsChallenge.created_at.desc(), SmsChallenge.id.desc()).limit(1))).scalar_one_or_none()
    if latest and (now - latest.created_at).total_seconds() < COOLDOWN:
        retry = max(1, COOLDOWN - int((now - latest.created_at).total_seconds()))
        raise HTTPException(429, "发送太频繁，请稍后再试", headers={"Retry-After": str(retry)})
    phone_count = await db.scalar(select(func.count()).select_from(SmsChallenge).where(
        SmsChallenge.phone_hash == phone_key, SmsChallenge.created_at > now - timedelta(days=1)))
    ip_count = await db.scalar(select(func.count()).select_from(SmsChallenge).where(
        SmsChallenge.ip_hash == ip_key, SmsChallenge.created_at > now - timedelta(hours=1)))
    if phone_count >= 10 or ip_count >= 30:
        raise HTTPException(429, "验证码发送次数已达上限，请稍后再试",
                            headers={"Retry-After": "3600"})
    challenge_id = uuid4().hex
    code = f"{secrets.randbelow(1000000):06d}"
    challenge = SmsChallenge(
        id=challenge_id, phone_hash=phone_key, ip_hash=ip_key,
        code_hash=digest(challenge_id + ":" + code), status="sending",
        attempts=0, created_at=now, expires_at=now + OTP_TTL,
    )
    db.add(challenge)
    # Reservation is committed before I/O: another worker cannot bypass limits.
    # The latest reservation also supersedes any older code immediately.
    await db.commit()
    try:
        await sms.send_code(phone, code)
    except Exception:
        await db.execute(update(SmsChallenge).where(SmsChallenge.id == challenge_id).values(
            status="failed", code_hash=""))
        await db.commit()
        raise HTTPException(503, "短信发送失败，请稍后重试") from None
    await db.execute(update(SmsChallenge).where(SmsChallenge.id == challenge_id).values(status="sent"))
    await db.commit()
    result = {"sent": True, "retry_after": COOLDOWN, "expires_in": int(OTP_TTL.total_seconds())}
    if settings.sms_provider == "mock" and settings.app_env == "development":
        result["dev_code"] = code
    return result


async def verify_code(db: AsyncSession, phone: str, code: str) -> None:
    phone_key = digest("phone:" + phone)
    await lock_keys(db, phone_key)
    challenge = (await db.execute(select(SmsChallenge).where(
        SmsChallenge.phone_hash == phone_key,
    ).order_by(SmsChallenge.created_at.desc(), SmsChallenge.id.desc()).limit(1).with_for_update()
    )).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if (challenge is None or challenge.status != "sent" or
            challenge.expires_at <= now or challenge.attempts >= MAX_ATTEMPTS):
        raise HTTPException(400, "验证码无效或已过期，请重新获取")
    challenge.attempts += 1
    if not hmac.compare_digest(challenge.code_hash, digest(challenge.id + ":" + code)):
        if challenge.attempts >= MAX_ATTEMPTS:
            challenge.status = "locked"
            challenge.code_hash = ""
        # Wrong attempts must survive the HTTP error / dependency rollback.
        await db.commit()
        raise HTTPException(400, "验证码错误或已失效")
    challenge.status = "consumed"
    challenge.code_hash = ""
    # Caller commits consumption, user creation and session atomically.
