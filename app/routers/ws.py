"""Authenticated realtime subscriptions, rechecked every ten seconds."""
import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal
from ..models import TripParticipant
from ..security import COOKIE_NAME, session_for_token
from ..services.ws import manager

router = APIRouter()


async def socket_member(token: str | None, trip_id: int) -> bool:
    async with SessionLocal() as db:
        session = await session_for_token(db, token)
        if session is None:
            return False
        return (await db.scalar(select(TripParticipant.id).where(
            TripParticipant.trip_id == trip_id,
            TripParticipant.user_id == session.user_id,
        ))) is not None


@router.websocket("/trips/{trip_id}/ws")
async def trip_ws(websocket: WebSocket, trip_id: int) -> None:
    origin = websocket.headers.get("origin")
    # WebSocket URL scheme differs from the browser Origin scheme.
    scheme = "https" if websocket.url.scheme == "wss" else "http"
    expected = f"{scheme}://{websocket.url.netloc}"
    if origin and origin != expected and origin not in settings.cors_origins:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    token = websocket.headers.get("x-token") or websocket.cookies.get(COOKIE_NAME)
    # Credentials in query strings can leak through proxy access logs.
    if not await socket_member(token, trip_id):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await manager.connect(trip_id, websocket)
    try:
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=10)
            except asyncio.TimeoutError:
                pass
            if not await socket_member(token, trip_id):
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                break
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(trip_id, websocket)
