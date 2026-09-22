"""本人偏好查看、关闭、清空和重建。"""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import DestinationFeedback, Trip, TripDestination, User
from ..schemas import UserPreferencesUpdate
from ..security import get_current_user
from ..services.preferences import build_derived_preferences, empty_derived_preferences, normalized_preferences

router = APIRouter(prefix="/users/me/preferences", tags=["preferences"])


@router.get("")
async def get_my_preferences(user: User = Depends(get_current_user)) -> dict:
    return normalized_preferences(user.preferences)


@router.put("")
async def update_my_preferences(
    body: UserPreferencesUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    current = normalized_preferences(user.preferences)
    user.preferences = {
        **current,
        "personalization_enabled": body.personalization_enabled,
        "explicit": body.explicit.model_dump(mode="json"),
    }
    await db.commit()
    return normalized_preferences(user.preferences)


@router.post("/rebuild")
async def rebuild_my_preferences(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(DestinationFeedback, TripDestination)
        .join(TripDestination, TripDestination.id == DestinationFeedback.destination_id)
        .join(Trip, Trip.id == DestinationFeedback.trip_id)
        .where(DestinationFeedback.user_id == user.id, Trip.status == "completed")
    )
    rows = [{
        "visited": feedback.visited,
        "rating": feedback.rating,
        "actual_stay_min": feedback.actual_stay_min,
        "tags": feedback.tags or [],
        "category": destination.category,
    } for feedback, destination in result.all()]
    current = normalized_preferences(user.preferences)
    user.preferences = {**current, "derived": build_derived_preferences(rows)}
    await db.commit()
    return normalized_preferences(user.preferences)


@router.delete("/derived")
async def clear_my_derived_preferences(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    current = normalized_preferences(user.preferences)
    user.preferences = {**current, "derived": empty_derived_preferences()}
    await db.commit()
    return normalized_preferences(user.preferences)
