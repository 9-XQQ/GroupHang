"""WebSocket 路由：客户端连入某个 trip 的房间，接收实时事件推送。"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from ..db import SessionLocal
from ..models import TripParticipant
from ..security import token_store
from ..services.ws import manager

router = APIRouter()


@router.websocket("/trips/{trip_id}/ws")
async def trip_ws(websocket: WebSocket, trip_id: int) -> None:
    """维持连接，接收服务器广播。

    浏览器 WebSocket 不能自定义认证 header，因此沿用现有登录 token 的查询参数。
    连接建立前校验 token 和 trip 成员身份。
    """
    token = websocket.query_params.get("token")
    user_id = token_store.get(token or "")
    if user_id is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="未登录或 token 无效")
        return
    async with SessionLocal() as db:
        result = await db.execute(
            select(TripParticipant.id).where(
                TripParticipant.trip_id == trip_id,
                TripParticipant.user_id == user_id,
            )
        )
        if result.scalar_one_or_none() is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="无权访问该 trip")
            return

    await manager.connect(trip_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(trip_id, websocket)
