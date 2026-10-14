"""本人偏好查看、关闭、清空和重建。"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import DestinationFeedback, Trip, TripDestination, User, PasswordCredential, AuthSession
from ..schemas import UserPreferencesUpdate, UserProfileUpdate
from ..security import get_current_user, request_token, token_digest
from ..services.preferences import build_derived_preferences, empty_derived_preferences, normalized_preferences

router = APIRouter(prefix="/users/me", tags=["preferences"])


@router.get("/preferences")
async def get_my_preferences(user: User = Depends(get_current_user)) -> dict:
    return normalized_preferences(user.preferences)


@router.put("/preferences")
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


@router.post("/preferences/rebuild")
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


@router.delete("/preferences/derived")
async def clear_my_derived_preferences(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    current = normalized_preferences(user.preferences)
    user.preferences = {**current, "derived": empty_derived_preferences()}
    await db.commit()
    return normalized_preferences(user.preferences)


@router.get("/profile")
async def get_my_profile(user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)) -> dict:
    credential = await db.get(PasswordCredential, user.id)
    return {"name": user.name, "username": credential.username if credential else None}


@router.patch("/profile")
async def update_my_profile(body: UserProfileUpdate,
                            user: User = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)) -> dict:
    user.name = body.name
    await db.commit()
    return {"name": user.name}


@router.get("/sessions")
async def get_my_sessions(response: Response, user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    count = await db.scalar(select(func.count()).select_from(AuthSession).where(
        AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None),
        AuthSession.expires_at > datetime.now(timezone.utc)))
    return {"active_sessions": count}


@router.post("/sessions/revoke-others")
async def revoke_other_sessions(request: Request, response: Response,
                                 user: User = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    now = datetime.now(timezone.utc)
    result = await db.execute(update(AuthSession).where(
        AuthSession.user_id == user.id,
        AuthSession.token_hash != token_digest(request_token(request)),
        AuthSession.revoked_at.is_(None), AuthSession.expires_at > now,
    ).values(revoked_at=now))
    await db.commit()
    return {"revoked_sessions": result.rowcount}
