"""Shared member-only destination comments; separate from private memories/feedback."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import DestinationComment, TripDestination, User
from ..schemas import DestinationCommentBody
from ..security import get_current_user
from ..services.access import require_trip_member
from ..services.ws import manager

router = APIRouter(prefix="/trips/{trip_id}/destinations/{destination_id}/comments", tags=["comments"])


async def member_destination(db, trip_id, destination_id, user):
    await require_trip_member(db, trip_id, user)
    place = await db.get(TripDestination, destination_id)
    if place is None or place.trip_id != trip_id:
        raise HTTPException(404, "地点不存在")


def serialize(comment, name):
    return {"id": comment.id, "user_id": comment.user_id, "name": name,
            "content": comment.content, "created_at": comment.created_at, "updated_at": comment.updated_at}


async def notify(trip_id, destination_id):
    await manager.broadcast(trip_id, {"type": "destination_comments_updated",
                                    "destination_id": destination_id})


@router.get("")
async def list_comments(trip_id: int, destination_id: int, offset: int = Query(default=0, ge=0),
                        user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await member_destination(db, trip_id, destination_id, user)
    rows = (await db.execute(select(DestinationComment, User.name).join(
        User, User.id == DestinationComment.user_id).where(
        DestinationComment.destination_id == destination_id).order_by(
        DestinationComment.id.desc()).offset(offset).limit(30))).all()
    return {"comments": [serialize(c, name) for c, name in rows], "offset": offset, "limit": 30}


@router.post("", status_code=201)
async def add_comment(trip_id: int, destination_id: int, body: DestinationCommentBody,
                      user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await member_destination(db, trip_id, destination_id, user)
    comment = DestinationComment(destination_id=destination_id, user_id=user.id, content=body.content)
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    await notify(trip_id, destination_id)
    return serialize(comment, user.name)


async def own_comment(db, destination_id, comment_id, user):
    comment = (await db.execute(select(DestinationComment).where(
        DestinationComment.id == comment_id, DestinationComment.destination_id == destination_id,
        DestinationComment.user_id == user.id).with_for_update())).scalar_one_or_none()
    if comment is None:
        raise HTTPException(404, "评论不存在或不属于本人")
    return comment


@router.put("/{comment_id}")
async def edit_comment(trip_id: int, destination_id: int, comment_id: int, body: DestinationCommentBody,
                       user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await member_destination(db, trip_id, destination_id, user)
    comment = await own_comment(db, destination_id, comment_id, user)
    comment.content = body.content
    await db.commit()
    await db.refresh(comment)
    await notify(trip_id, destination_id)
    return serialize(comment, user.name)


@router.delete("/{comment_id}")
async def delete_comment(trip_id: int, destination_id: int, comment_id: int,
                         user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await member_destination(db, trip_id, destination_id, user)
    comment = await own_comment(db, destination_id, comment_id, user)
    await db.delete(comment)
    await db.commit()
    await notify(trip_id, destination_id)
    return {"deleted": True}
