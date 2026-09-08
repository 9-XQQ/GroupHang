"""Phase 2A：trip 候选地点协作接口。"""
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Trip, TripDestination, TripVote, User
from ..schemas import DestinationCreate, DestinationStatusUpdate, DestinationUpdate
from ..security import get_current_user
from ..services.access import require_trip_member
from ..services.ws import manager

router = APIRouter(prefix="/trips/{trip_id}/destinations", tags=["destinations"])


def _destination_dict(destination: TripDestination, submitter: User | None, votes: dict) -> dict:
    return {
        "id": destination.id,
        "trip_id": destination.trip_id,
        "name": destination.name,
        "address": destination.address,
        "lat": destination.lat,
        "lng": destination.lng,
        "category": destination.category,
        "visit_status": destination.visit_status,
        "expected_stay_min": destination.expected_stay_min,
        "opening_hours": destination.opening_hours,
        "note": destination.note,
        "submitted_by": {
            "user_id": destination.submitted_by,
            "name": submitter.name if submitter else "未知",
        },
        "votes": votes,
        "created_at": destination.created_at,
        "updated_at": destination.updated_at,
    }


async def _load_destination(db: AsyncSession, trip_id: int, destination_id: int) -> TripDestination:
    destination = await db.get(TripDestination, destination_id)
    if destination is None or destination.trip_id != trip_id:
        raise HTTPException(status_code=404, detail="地点不存在")
    return destination


async def _bump_input_version(db: AsyncSession, trip_id: int) -> int:
    trip = await db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="trip 不存在")
    trip.input_version = (trip.input_version or 0) + 1
    return trip.input_version


async def _broadcast_update(trip_id: int, input_version: int) -> None:
    await manager.broadcast(
        trip_id,
        {"type": "destinations_updated", "trip_id": trip_id, "input_version": input_version},
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_destination(
    trip_id: int,
    body: DestinationCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_trip_member(db, trip_id, user)
    destination = TripDestination(
        trip_id=trip_id,
        submitted_by=user.id,
        **body.model_dump(mode="json"),
    )
    db.add(destination)
    input_version = await _bump_input_version(db, trip_id)
    await db.commit()
    await db.refresh(destination)
    await _broadcast_update(trip_id, input_version)
    return _destination_dict(destination, user, {"up": 0, "down": 0, "my_vote": 0})


@router.get("")
async def list_destinations(
    trip_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_trip_member(db, trip_id, user)
    trip = await db.get(Trip, trip_id)
    result = await db.execute(
        select(TripDestination)
        .where(TripDestination.trip_id == trip_id)
        .order_by(TripDestination.created_at.asc(), TripDestination.id.asc())
    )
    destinations = list(result.scalars().all())

    vote_result = await db.execute(
        select(TripVote).where(
            TripVote.trip_id == trip_id,
            TripVote.candidate_type == "destination",
        )
    )
    vote_map: dict[str, dict] = {}
    for vote in vote_result.scalars().all():
        counts = vote_map.setdefault(vote.candidate_id, {"up": 0, "down": 0, "my_vote": 0})
        if vote.vote_value > 0:
            counts["up"] += 1
        elif vote.vote_value < 0:
            counts["down"] += 1
        if vote.user_id == user.id:
            counts["my_vote"] = vote.vote_value

    submitter_ids = {destination.submitted_by for destination in destinations}
    users: dict[int, User] = {}
    if submitter_ids:
        user_result = await db.execute(select(User).where(User.id.in_(submitter_ids)))
        users = {row.id: row for row in user_result.scalars().all()}

    return {
        "input_version": trip.input_version if trip else 1,
        "destinations": [
            _destination_dict(
                destination,
                users.get(destination.submitted_by),
                vote_map.get(str(destination.id), {"up": 0, "down": 0, "my_vote": 0}),
            )
            for destination in destinations
        ],
    }


@router.put("/{destination_id}")
async def update_destination(
    trip_id: int,
    destination_id: int,
    body: DestinationUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    participant = await require_trip_member(db, trip_id, user)
    destination = await _load_destination(db, trip_id, destination_id)
    if participant.role != "creator" and destination.submitted_by != user.id:
        raise HTTPException(status_code=403, detail="只能编辑自己提交的地点")
    changes = body.model_dump(exclude_unset=True, mode="json")
    if not changes:
        raise HTTPException(status_code=400, detail="没有可更新的字段")
    structural_fields = {"name", "address", "lat", "lng", "category"}
    if destination.visit_status != "candidate" and structural_fields.intersection(changes):
        raise HTTPException(status_code=409, detail="已确认地点只能修改停留时间、营业时间和备注；修改位置或名称前请先改回候选")
    for field, value in changes.items():
        setattr(destination, field, value)
    input_version = await _bump_input_version(db, trip_id)
    await db.commit()
    await db.refresh(destination)
    submitter = await db.get(User, destination.submitted_by)
    await _broadcast_update(trip_id, input_version)
    return _destination_dict(destination, submitter, {"up": 0, "down": 0, "my_vote": 0})


@router.delete("/{destination_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_destination(
    trip_id: int,
    destination_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    participant = await require_trip_member(db, trip_id, user)
    destination = await _load_destination(db, trip_id, destination_id)
    if participant.role != "creator" and destination.submitted_by != user.id:
        raise HTTPException(status_code=403, detail="只能删除自己提交的地点")
    if destination.visit_status != "candidate":
        raise HTTPException(status_code=409, detail="已确认状态的地点不能删除，请改为本次不去")

    await db.execute(
        delete(TripVote).where(
            TripVote.trip_id == trip_id,
            TripVote.candidate_type == "destination",
            TripVote.candidate_id == str(destination_id),
        )
    )
    await db.delete(destination)
    input_version = await _bump_input_version(db, trip_id)
    await db.commit()
    await _broadcast_update(trip_id, input_version)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{destination_id}/status")
async def update_destination_status(
    trip_id: int,
    destination_id: int,
    body: DestinationStatusUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    participant = await require_trip_member(db, trip_id, user)
    if participant.role != "creator":
        raise HTTPException(status_code=403, detail="只有发起人可以确认地点状态")
    destination = await _load_destination(db, trip_id, destination_id)
    destination.visit_status = body.visit_status
    input_version = await _bump_input_version(db, trip_id)
    await db.commit()
    await db.refresh(destination)
    submitter = await db.get(User, destination.submitted_by)
    await _broadcast_update(trip_id, input_version)
    return _destination_dict(destination, submitter, {"up": 0, "down": 0, "my_vote": 0})
