"""WebSocket 房间管理：按 trip_id 分组维护连接，广播事件。

事件只发「信号」（type + trip_id），不带业务数据；
前端收到信号后自行调用对应 GET 接口刷新，保证数据单一来源、不产生不一致。
"""
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        # trip_id -> 该 trip 的活跃 WebSocket 连接集合
        self.rooms: dict[int, set[WebSocket]] = {}

    async def connect(self, trip_id: int, ws: WebSocket) -> None:
        await ws.accept()
        self.rooms.setdefault(trip_id, set()).add(ws)

    def disconnect(self, trip_id: int, ws: WebSocket) -> None:
        room = self.rooms.get(trip_id)
        if room is None:
            return
        room.discard(ws)
        if not room:
            self.rooms.pop(trip_id, None)

    async def broadcast(self, trip_id: int, message: dict[str, Any]) -> None:
        room = list(self.rooms.get(trip_id, set()))
        dead: list[WebSocket] = []
        for ws in room:
            try:
                await ws.send_json(message)
            except Exception:
                # 连接已断开（send 抛异常），记下来统一清理
                dead.append(ws)
        for ws in dead:
            self.disconnect(trip_id, ws)


manager = ConnectionManager()
