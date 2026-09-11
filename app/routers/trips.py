"""Trip 路由：创建、加入、查看详情。"""
import secrets
import string

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import MeetingPointResult, Trip, TripParticipant, User
from ..schemas import JoinRequest, TripCreate
from ..security import get_current_user
from ..services.access import require_trip_member
from ..services.ws import manager

router = APIRouter(prefix="/trips", tags=["trips"])


def _gen_code(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


@router.get("")
async def list_my_trips(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """列出当前账号创建或加入过的行程，由用户决定是否恢复。"""
    result = await db.execute(
        select(Trip, TripParticipant)
        .join(TripParticipant, TripParticipant.trip_id == Trip.id)
        .where(TripParticipant.user_id == user.id)
        .order_by(Trip.created_at.desc(), Trip.id.desc())
    )
    return {"trips": [{
        "trip_id": trip.id, "title": trip.title, "status": trip.status,
        "role": participant.role, "invite_code": trip.invite_code,
        "planned_start_at": trip.planned_start_at, "planned_end_at": trip.planned_end_at,
        "joined_at": participant.joined_at, "created_at": trip.created_at,
    } for trip, participant in result.all()]}


@router.post("", status_code=201)
async def create_trip(
    body: TripCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    code = _gen_code()
    trip = Trip(title=body.title, creator_id=user.id, invite_code=code, default_mode=body.default_mode)
    db.add(trip)
    await db.flush()  # 拿到 trip.id

    # 发起人自动成为 creator 参与者
    db.add(TripParticipant(trip_id=trip.id, user_id=user.id, role="creator", transport_mode=body.default_mode))
    await db.commit()

    return {
        "trip_id": trip.id,
        "invite_code": code,
        "invite_url": f"/trips/join?code={code}",
        "creator_id": user.id,
    }


async def _join(db: AsyncSession, trip: Trip, user: User) -> dict:
    """加入 trip（幂等），返回 {trip_id, participant_id, role}。"""
    result = await db.execute(
        select(TripParticipant).where(
            TripParticipant.trip_id == trip.id, TripParticipant.user_id == user.id
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        return {"trip_id": trip.id, "participant_id": existing.id, "role": existing.role}
    if trip.status != "active":
        raise HTTPException(status_code=409, detail="trip 已确认，暂不接受新成员加入")

    participant = TripParticipant(
        trip_id=trip.id, user_id=user.id, role="member", transport_mode=trip.default_mode
    )
    db.add(participant)
    await db.commit()
    await db.refresh(participant)

    await manager.broadcast(trip.id, {"type": "participants_updated", "trip_id": trip.id})

    return {"trip_id": trip.id, "participant_id": participant.id, "role": participant.role}


@router.post("/{trip_id}/join")
async def join_trip(
    trip_id: int,
    body: JoinRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    trip = await db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="trip 不存在")
    if trip.invite_code != body.invite_code:
        raise HTTPException(status_code=400, detail="邀请码无效")
    return await _join(db, trip, user)


@router.post("/join-by-code")
async def join_by_code(
    body: JoinRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """仅凭邀请码加入（前端拿到分享链接时通常只知道 code，不知道 trip_id）。"""
    result = await db.execute(select(Trip).where(Trip.invite_code == body.invite_code))
    trip = result.scalar_one_or_none()
    if trip is None:
        raise HTTPException(status_code=400, detail="邀请码无效")
    return await _join(db, trip, user)


@router.get("/{trip_id}")
async def get_trip(
    trip_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    trip = await db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="trip 不存在")
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

    mp_result = await db.execute(
        select(MeetingPointResult)
        .where(MeetingPointResult.trip_id == trip_id)
        .order_by(MeetingPointResult.computed_at.desc())
        .limit(1)
    )
    latest_mp = mp_result.scalar_one_or_none()

    return {
        "trip_id": trip.id,
        "title": trip.title,
        "status": trip.status,
        "my_role": next((p.role for p in parts if p.user_id == user.id), "member"),
        "invite_code": trip.invite_code if trip.creator_id == user.id else None,
        "default_mode": trip.default_mode,
        "participants": participants,
        "meeting_points_ready": latest_mp is not None,
        "last_computed_at": latest_mp.computed_at if latest_mp else None,
    }


@router.post("/{trip_id}/reopen")
async def reopen_trip(
    trip_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    participant = await require_trip_member(db, trip_id, user)
    if participant.role != "creator":
        raise HTTPException(status_code=403, detail="只有发起人可以重新编辑 trip")
    trip = await db.get(Trip, trip_id)
    if trip.status == "active":
        return {"trip_id": trip.id, "status": trip.status, "input_version": trip.input_version}
    trip.status = "active"
    trip.input_version = (trip.input_version or 0) + 1
    await db.commit()
    await manager.broadcast(trip_id, {"type": "trip_reopened", "trip_id": trip_id, "input_version": trip.input_version})
    return {"trip_id": trip.id, "status": trip.status, "input_version": trip.input_version}


@router.delete("/{trip_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trip(
    trip_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    participant = await require_trip_member(db, trip_id, user)
    if participant.role != "creator":
        raise HTTPException(status_code=403, detail="只有发起人可以删除 trip")
    trip = await db.get(Trip, trip_id)
    await db.delete(trip)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
