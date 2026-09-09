"""参与者路由：更新自己的出发点 + 出行方式。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import TripParticipant, User
from ..schemas import ParticipantUpdate
from ..security import get_current_user
from ..services.access import require_trip_member
from ..services.ws import manager

router = APIRouter(prefix="/trips/{trip_id}/participants", tags=["participants"])


@router.put("/me")
async def update_me(
    trip_id: int,
    body: ParticipantUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_trip_member(db, trip_id, user)
    result = await db.execute(
        select(TripParticipant).where(
            TripParticipant.trip_id == trip_id, TripParticipant.user_id == user.id
        )
    )
    participant = result.scalar_one_or_none()
    if participant is None:
        raise HTTPException(status_code=404, detail="你尚未加入该 trip")

    participant.start_location = {
        "lat": body.start_location.lat,
        "lng": body.start_location.lng,
        "address": body.start_location.address,
    }
    participant.transport_mode = body.transport_mode
    participant.available_from = body.available_from
    participant.available_until = body.available_until
    participant.vote_status = "submitted"
    await db.commit()

    await manager.broadcast(trip_id, {"type": "participants_updated", "trip_id": trip_id})

    return {
        "participant_id": participant.id,
        "start_location": participant.start_location,
        "transport_mode": participant.transport_mode,
        "available_from": participant.available_from,
        "available_until": participant.available_until,
    }


@router.get("")
async def list_participants(
    trip_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_trip_member(db, trip_id, user)
    result = await db.execute(
        select(TripParticipant).where(TripParticipant.trip_id == trip_id)
    )
    parts = result.scalars().all()

    participants = []
    for p in parts:
        u = await db.get(User, p.user_id)
        participants.append(
            {
                "user_id": p.user_id,
                "name": u.name if u else "未知",
                "role": p.role,
                "start_location": p.start_location,
                "transport_mode": p.transport_mode,
                "vote_status": p.vote_status,
                "available_from": p.available_from,
                "available_until": p.available_until,
            }
        )
    return {"participants": participants}
