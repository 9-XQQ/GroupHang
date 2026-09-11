"""Trip 资源访问控制。"""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Trip, TripParticipant, User


async def require_trip_member(db: AsyncSession, trip_id: int, user: User) -> TripParticipant:
    """要求当前用户是 trip 成员；同时避免向非成员泄露 trip 内容。"""
    trip = await db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="trip 不存在")

    result = await db.execute(
        select(TripParticipant).where(
            TripParticipant.trip_id == trip_id,
            TripParticipant.user_id == user.id,
        )
    )
    participant = result.scalar_one_or_none()
    if participant is None:
        raise HTTPException(status_code=403, detail="你不是该 trip 的参与者")
    return participant


async def require_trip_active(db: AsyncSession, trip_id: int) -> Trip:
    """要求 trip 尚未确认锁定；确认后的修改必须先由创建者重新开启。"""
    trip = await db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="trip 不存在")
    if trip.status != "active":
        raise HTTPException(status_code=409, detail="trip 已确认锁定，请由发起人点击“重新编辑”后再修改")
    return trip
