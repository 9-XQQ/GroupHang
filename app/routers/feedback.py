"""Phase 4A-2：已完成行程的地点个人反馈。"""
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import DestinationFeedback, Trip, TripDestination, User
from ..schemas import DestinationFeedbackUpdate
from ..security import get_current_user
from ..services.access import require_trip_member
from ..services.ws import manager

router = APIRouter(prefix="/trips/{trip_id}", tags=["feedback"])


def _my_feedback(row: DestinationFeedback | None) -> dict | None:
    if row is None:
        return None
    return {
        "visited": row.visited,
        "rating": row.rating,
        "actual_stay_min": row.actual_stay_min,
        "tags": row.tags or [],
        "comment": row.comment,
        "would_revisit": row.would_revisit,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@router.get("/feedback")
async def get_trip_feedback(
    trip_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_trip_member(db, trip_id, user)
    trip = await db.get(Trip, trip_id)
    destination_result = await db.execute(
        select(TripDestination)
        .where(TripDestination.trip_id == trip_id)
        .order_by(TripDestination.created_at.asc(), TripDestination.id.asc())
    )
    destinations = list(destination_result.scalars().all())
    feedback_result = await db.execute(
        select(DestinationFeedback).where(DestinationFeedback.trip_id == trip_id)
    )
    rows = list(feedback_result.scalars().all())
    by_destination: dict[int, list[DestinationFeedback]] = {}
    for row in rows:
        by_destination.setdefault(row.destination_id, []).append(row)

    items = []
    for destination in destinations:
        feedback_rows = by_destination.get(destination.id, [])
        ratings = [row.rating for row in feedback_rows if row.rating is not None]
        tag_counts = Counter(tag for row in feedback_rows for tag in (row.tags or []))
        mine = next((row for row in feedback_rows if row.user_id == user.id), None)
        items.append({
            "destination_id": destination.id,
            "destination_name": destination.name,
            "summary": {
                "feedback_count": len(feedback_rows),
                "visited_count": sum(1 for row in feedback_rows if row.visited),
                "average_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
                "rating_count": len(ratings),
                "tag_counts": dict(tag_counts.most_common()),
            },
            "my_feedback": _my_feedback(mine),
        })
    return {"trip_id": trip_id, "status": trip.status, "destinations": items}


@router.put("/destinations/{destination_id}/feedback/me")
async def upsert_my_destination_feedback(
    trip_id: int,
    destination_id: int,
    body: DestinationFeedbackUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_trip_member(db, trip_id, user)
    trip = await db.get(Trip, trip_id)
    if trip.status != "completed":
        raise HTTPException(status_code=409, detail="只有已完成的 trip 可以提交地点反馈")
    destination = await db.get(TripDestination, destination_id)
    if destination is None or destination.trip_id != trip_id:
        raise HTTPException(status_code=404, detail="地点不存在")
    result = await db.execute(
        select(DestinationFeedback).where(
            DestinationFeedback.trip_id == trip_id,
            DestinationFeedback.destination_id == destination_id,
            DestinationFeedback.user_id == user.id,
        )
    )
    row = result.scalar_one_or_none()
    values = body.model_dump()
    if row is None:
        row = DestinationFeedback(
            trip_id=trip_id, destination_id=destination_id, user_id=user.id, **values
        )
        db.add(row)
    else:
        for field, value in values.items():
            setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    await manager.broadcast(trip_id, {"type": "destination_feedback_updated", "trip_id": trip_id})
    return {"destination_id": destination_id, "my_feedback": _my_feedback(row)}
