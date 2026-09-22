"""Phase 4A-3：记录脱敏的地点解析接受、修改和拒绝动作。"""
from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import PlaceParseFeedback, User
from ..schemas import PlaceParseFeedbackCreate
from ..security import get_current_user
from ..services.access import require_trip_member

router = APIRouter(prefix="/trips/{trip_id}/place-parse-feedback", tags=["place-parse-feedback"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_place_parse_feedback(
    trip_id: int,
    body: PlaceParseFeedbackCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_trip_member(db, trip_id, user)
    session_id, candidate_id = str(body.parse_session_id), str(body.candidate_id)
    result = await db.execute(select(PlaceParseFeedback).where(
        PlaceParseFeedback.trip_id == trip_id,
        PlaceParseFeedback.user_id == user.id,
        PlaceParseFeedback.parse_session_id == session_id,
        PlaceParseFeedback.candidate_id == candidate_id,
    ))
    existing = result.scalar_one_or_none()
    if existing:
        return {"id": existing.id, "action": existing.action, "recorded": True, "duplicate": True}

    feedback = PlaceParseFeedback(
        trip_id=trip_id,
        user_id=user.id,
        parse_session_id=session_id,
        candidate_id=candidate_id,
        parser=body.parser,
        action=body.action,
        proposed_place=body.proposed_place.model_dump(mode="json"),
        final_place=body.final_place.model_dump(mode="json") if body.final_place else None,
        consent_to_improve=body.consent_to_improve,
    )
    db.add(feedback)
    await db.commit()
    await db.refresh(feedback)
    return {"id": feedback.id, "action": feedback.action, "recorded": True, "duplicate": False}
