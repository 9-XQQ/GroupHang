"""Database-backed opaque sessions; legacy token files are never trusted."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .models import AuthSession, User

COOKIE_NAME = "trip_session"


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue_token(db: AsyncSession, user_id: int) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=settings.auth_session_days)
    db.add(AuthSession(token_hash=token_digest(token), user_id=user_id, expires_at=expires))
    return token, expires


async def session_for_token(db: AsyncSession, token: str | None) -> AuthSession | None:
    if not token or len(token) > 256:
        return None
    return (await db.execute(select(AuthSession).where(
        AuthSession.token_hash == token_digest(token),
        AuthSession.revoked_at.is_(None),
        AuthSession.expires_at > datetime.now(timezone.utc),
    ))).scalar_one_or_none()


def request_token(request: Request) -> str | None:
    return request.headers.get("x-token") or request.cookies.get(COOKIE_NAME)


def check_browser_origin(request: Request) -> None:
    """Custom header + origin validation protects cookie and login mutations."""
    origin = request.headers.get("origin")
    expected = str(request.base_url).rstrip("/")
    if origin and origin != expected and origin not in settings.cors_origins:
        raise HTTPException(403, "请求来源不受信任")
    if request.cookies.get(COOKIE_NAME) and request.method not in {"GET", "HEAD", "OPTIONS"}:
        if request.headers.get("x-requested-with") != "TripPlanner":
            raise HTTPException(403, "缺少请求校验头")


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    check_browser_origin(request)
    session = await session_for_token(db, request_token(request))
    if session is None:
        raise HTTPException(401, "登录已过期，请重新登录")
    user = await db.get(User, session.user_id)
    if user is None:
        raise HTTPException(401, "用户不存在")
    return user
