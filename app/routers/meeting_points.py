"""约点推荐路由：触发计算 + 获取结果。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import MeetingPointResult, Trip, TripParticipant, User
from ..schemas import MeetingPointRequest
from ..security import get_current_user
from ..services.access import require_trip_member
from ..services.meeting_point import recommend
from ..services.ws import manager

router = APIRouter(prefix="/trips/{trip_id}", tags=["meeting-points"])


async def _load_participants(trip_id: int, db: AsyncSession) -> list[dict]:
    result = await db.execute(
        select(TripParticipant).where(TripParticipant.trip_id == trip_id)
    )
    parts = result.scalars().all()
    if len(parts) < 2:
        raise HTTPException(status_code=409, detail="至少需要 2 名参与者")

    participants = []
    missing = []
    for p in parts:
        u = await db.get(User, p.user_id)
        if p.start_location is None:
            missing.append(u.name if u else p.user_id)
            continue
        loc = p.start_location
        participants.append(
            {
                "user_id": p.user_id,
                "name": u.name if u else "未知",
                "mode": p.transport_mode,
                "lat": float(loc["lat"]),
                "lng": float(loc["lng"]),
            }
        )

    if missing:
        raise HTTPException(
            status_code=409,
            detail=(
                f"以下参与者尚未填写出发点：{missing}。"
                "每位参与者需用各自的账号登录，提交自己的出发点后，才能生成推荐。"
            ),
        )
    return participants


@router.post("/meeting-points")
async def compute_meeting_points(
    trip_id: int,
    body: MeetingPointRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    trip = await db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="trip 不存在")
    await require_trip_member(db, trip_id, user)

    participants = await _load_participants(trip_id, db)
    objective = body.objective if body else None
    candidates = await recommend(participants, objective)

    if not candidates:
        raise HTTPException(status_code=500, detail="候选点生成失败")

    db.add(MeetingPointResult(trip_id=trip_id, candidates=candidates, objective=objective))
    await db.commit()

    await manager.broadcast(trip_id, {"type": "meeting_points_ready", "trip_id": trip_id})

    return {"trip_id": trip_id, "objective": objective, "candidates": candidates}


@router.get("/meeting-points")
async def get_meeting_points(
    trip_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_trip_member(db, trip_id, user)
    result = await db.execute(
        select(MeetingPointResult)
        .where(MeetingPointResult.trip_id == trip_id)
        .order_by(MeetingPointResult.computed_at.desc())
        .limit(1)
    )
    latest = result.scalar_one_or_none()
    if latest is None:
        raise HTTPException(status_code=404, detail="尚未计算推荐结果")

    return {
        "trip_id": trip_id,
        "objective": latest.objective,
        "computed_at": latest.computed_at,
        "candidates": latest.candidates,
    }
