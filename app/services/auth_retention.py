"""Logical expiry is checked on every use; sweeps bound metadata retention."""
from datetime import timedelta

from sqlalchemy import delete, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuthRateLimit, AuthSession, SmsChallenge


async def cleanup_auth_data(db: AsyncSession) -> None:
    clock = func.now()
    await db.execute(delete(AuthRateLimit).where(
        AuthRateLimit.window_started_at <= clock - timedelta(days=1),
    ))
    await db.execute(update(SmsChallenge).where(
        SmsChallenge.expires_at <= clock, SmsChallenge.code_hash != "",
    ).values(code_hash="", status="expired"))
    # Preserve quota evidence for the rolling 24-hour phone window.
    await db.execute(delete(SmsChallenge).where(
        SmsChallenge.created_at <= clock - timedelta(days=2),
    ))
    await db.execute(delete(AuthSession).where(
        (AuthSession.expires_at <= clock) | (AuthSession.revoked_at.is_not(None)),
    ))
