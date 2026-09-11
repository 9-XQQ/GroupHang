"""投票路由：轻量点赞/踩，按候选点聚合票数。"""
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import MeetingPointResult, TripDestination, TripVote, User
from ..schemas import VoteRequest
from ..security import get_current_user
from ..services.access import require_trip_active, require_trip_member
from ..services.ws import manager

router = APIRouter(prefix="/trips/{trip_id}/votes", tags=["votes"])


def _stored_candidate_id(candidate: dict) -> str:
    """兼容补充 candidate_id 之前已经存入数据库的推荐快照。"""
    if candidate.get("candidate_id"):
        return str(candidate["candidate_id"])
    poi = candidate.get("poi", {})
    return f"coord:{float(poi['lat']):.6f},{float(poi['lng']):.6f}"


async def _require_current_candidate(
    db: AsyncSession, trip_id: int, candidate_type: str, candidate_id: str
) -> None:
    if candidate_type == "destination":
        try:
            destination_id = int(candidate_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="地点 candidate_id 必须是数字 ID") from exc
        destination = await db.get(TripDestination, destination_id)
        if destination is None or destination.trip_id != trip_id:
            raise HTTPException(status_code=400, detail="候选地点不存在")
        return
    if candidate_type == "poi":
        raise HTTPException(status_code=400, detail="Phase 2 暂不支持独立 POI 投票，请先加入想去地点")

    result = await db.execute(
        select(MeetingPointResult)
        .where(MeetingPointResult.trip_id == trip_id)
        .order_by(MeetingPointResult.computed_at.desc())
        .limit(1)
    )
    latest = result.scalar_one_or_none()
    if latest is None or candidate_id not in {_stored_candidate_id(c) for c in latest.candidates}:
        raise HTTPException(status_code=400, detail="候选点不存在或推荐结果已更新")


def aggregate_votes(rows: list[TripVote], current_user_id: int) -> list[dict]:
    """聚合候选票数，并附上当前用户自己的选择。"""
    agg: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"up": 0, "down": 0})
    my_votes: dict[tuple[str, str], int] = {}
    for vote_row in rows:
        key = (vote_row.candidate_type, vote_row.candidate_id)
        if vote_row.vote_value > 0:
            agg[key]["up"] += 1
        elif vote_row.vote_value < 0:
            agg[key]["down"] += 1
        if vote_row.user_id == current_user_id:
            my_votes[key] = vote_row.vote_value
    return [
        {
            "candidate_type": key[0],
            "candidate_id": key[1],
            "up": counts["up"],
            "down": counts["down"],
            "my_vote": my_votes.get(key, 0),
        }
        for key, counts in agg.items()
    ]


@router.post("")
async def vote(
    trip_id: int,
    body: VoteRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_trip_member(db, trip_id, user)
    await require_trip_active(db, trip_id)
    await _require_current_candidate(db, trip_id, body.candidate_type, body.candidate_id)
    result = await db.execute(
        select(TripVote).where(
            TripVote.trip_id == trip_id,
            TripVote.candidate_type == body.candidate_type,
            TripVote.candidate_id == body.candidate_id,
            TripVote.user_id == user.id,
        )
    )
    existing = result.scalar_one_or_none()

    if body.vote_value == 0:
        # 取消投票
        if existing is not None:
            await db.delete(existing)
            await db.commit()
        event_type = "destination_votes_updated" if body.candidate_type == "destination" else "votes_updated"
        await manager.broadcast(trip_id, {"type": event_type, "trip_id": trip_id})
        return {"ok": True}

    if existing is not None:
        existing.vote_value = body.vote_value
    else:
        db.add(
            TripVote(
                trip_id=trip_id,
                candidate_type=body.candidate_type,
                candidate_id=body.candidate_id,
                user_id=user.id,
                vote_value=body.vote_value,
            )
        )
    await db.commit()
    event_type = "destination_votes_updated" if body.candidate_type == "destination" else "votes_updated"
    await manager.broadcast(trip_id, {"type": event_type, "trip_id": trip_id})
    return {"ok": True}


@router.get("")
async def list_votes(
    trip_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_trip_member(db, trip_id, user)
    result = await db.execute(select(TripVote).where(TripVote.trip_id == trip_id))
    rows = result.scalars().all()

    return {"results": aggregate_votes(list(rows), user.id)}
