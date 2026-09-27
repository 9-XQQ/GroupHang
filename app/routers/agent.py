"""Phase 5：读取 Trip 上下文、生成受控草案并在确认后执行。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import ItineraryPlan, Trip, TripDestination, TripParticipant, User
from ..schemas import AgentChatRequest, AgentProposalExecuteRequest, PlanningSettingsUpdate, SharedTextParseRequest
from ..security import get_current_user
from ..services.access import require_trip_active, require_trip_member
from ..services.agent_assistant import AgentAction, agent_assistant
from ..services.llm_parser import llm_place_parser
from ..services.ws import manager
from .itineraries import create_itinerary_plan
from .shared_text import parse_text

router = APIRouter(prefix="/trips/{trip_id}/assistant", tags=["assistant"])


async def _context(db: AsyncSession, trip: Trip, user: User) -> dict:
    participant_rows = list((await db.execute(
        select(TripParticipant, User)
        .join(User, User.id == TripParticipant.user_id)
        .where(TripParticipant.trip_id == trip.id)
    )).all())
    destinations = list((await db.execute(
        select(TripDestination).where(TripDestination.trip_id == trip.id).order_by(TripDestination.id)
    )).scalars().all())
    latest_plan = (await db.execute(
        select(ItineraryPlan).where(ItineraryPlan.trip_id == trip.id)
        .order_by(ItineraryPlan.computed_at.desc(), ItineraryPlan.id.desc()).limit(1)
    )).scalar_one_or_none()
    return {
        "trip": {
            "title": trip.title, "status": trip.status, "primary_workflow": trip.primary_workflow,
            "planned_start_at": trip.planned_start_at, "planned_end_at": trip.planned_end_at,
            "group_transport_mode": trip.group_transport_mode,
            "optimization_objective": trip.optimization_objective,
            "input_version": trip.input_version,
        },
        "participants": [{
            "name": user.name, "role": participant.role,
            "transport_mode": participant.transport_mode,
            "has_start_location": bool(participant.start_location),
            "available_from": participant.available_from, "available_until": participant.available_until,
        } for participant, user in participant_rows],
        "destinations": [{
            "id": item.id, "name": item.name, "category": item.category,
            "visit_status": item.visit_status, "expected_stay_min": item.expected_stay_min,
        } for item in destinations],
        "latest_plan": None if latest_plan is None else {
            "status": latest_plan.status,
            "is_stale": latest_plan.input_version != trip.input_version,
            "ordered_stops": latest_plan.ordered_stops,
            "warnings": latest_plan.warnings,
            "total_travel_min": latest_plan.total_travel_min,
            "total_duration_min": latest_plan.total_duration_min,
        },
        "my_preferences": {
            "personalization_enabled": bool((user.preferences or {}).get("personalization_enabled", True)),
            "explicit": (user.preferences or {}).get("explicit", {}),
            "derived": (user.preferences or {}).get("derived", {}),
        },
    }


@router.post("/chat")
async def chat(
    trip_id: int, body: AgentChatRequest,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    participant = await require_trip_member(db, trip_id, user)
    trip = await db.get(Trip, trip_id)
    answer, llm_used, warning = await agent_assistant.chat(
        await _context(db, trip, user), body.message, trip_id, user.id,
    )
    actions = answer.actions if participant.role == "creator" and trip.status == "active" else []
    proposal_id = agent_assistant.store(trip_id, user.id, trip.input_version, actions)
    if answer.actions and not actions:
        warning = "只有规划中的 Trip 发起人可以执行助手草案；本次仅展示建议"
    return {
        "reply": answer.reply,
        "llm_used": llm_used,
        "llm_usage": llm_place_parser.last_usage,
        "warning": warning,
        "proposal_id": proposal_id,
        "actions": [action.model_dump(mode="json") for action in actions],
        "requires_confirmation": bool(proposal_id),
    }


@router.delete("/history")
async def clear_history(
    trip_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    await require_trip_member(db, trip_id, user)
    agent_assistant.clear_history(trip_id, user.id)
    return {"cleared": True}


@router.post("/proposals/execute")
async def execute_proposal(
    trip_id: int, body: AgentProposalExecuteRequest,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    participant = await require_trip_member(db, trip_id, user)
    if participant.role != "creator":
        raise HTTPException(status_code=403, detail="只有发起人可以执行助手草案")
    trip = await require_trip_active(db, trip_id)
    proposal = agent_assistant.take(str(body.proposal_id), trip_id, user.id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="草案不存在或已过期，请重新询问助手")
    if proposal.input_version != trip.input_version:
        raise HTTPException(status_code=409, detail="Trip 已发生变化，请重新询问助手后再确认")

    executed = []
    place_import = None
    itinerary = None
    for raw_action in proposal.actions:
        action = AgentAction.model_validate(raw_action)
        if action.type == "switch_workflow":
            trip.primary_workflow = action.primary_workflow
            executed.append({"type": action.type, "primary_workflow": action.primary_workflow})
        elif action.type == "update_planning_settings":
            if trip.primary_workflow != "itinerary":
                raise HTTPException(status_code=409, detail="请先把 Trip 主方案切换为多地点路线")
            settings = PlanningSettingsUpdate(
                planned_start_at=action.planned_start_at,
                planned_end_at=action.planned_end_at,
                group_transport_mode=action.group_transport_mode or trip.group_transport_mode,
                optimization_objective=action.optimization_objective or trip.optimization_objective,
            )
            trip.planned_start_at = settings.planned_start_at
            trip.planned_end_at = settings.planned_end_at
            trip.group_transport_mode = settings.group_transport_mode
            trip.optimization_objective = settings.optimization_objective
            trip.input_version = (trip.input_version or 0) + 1
            executed.append({"type": action.type})
        elif action.type == "prepare_place_import":
            place_import = await parse_text(
                trip_id,
                SharedTextParseRequest(text=action.import_text, use_llm=True),
                user,
                db,
            )
            executed.append({"type": action.type, "candidate_count": len(place_import.get("candidates", []))})
        elif action.type == "generate_itinerary":
            itinerary = await create_itinerary_plan(trip_id, user, db)
            executed.append({"type": action.type, "plan_id": itinerary.get("plan_id")})
    await db.commit()
    await manager.broadcast(trip_id, {
        "type": "assistant_actions_executed", "trip_id": trip_id,
        "input_version": trip.input_version, "primary_workflow": trip.primary_workflow,
    })
    return {
        "trip_id": trip_id, "executed": executed, "input_version": trip.input_version,
        "place_import": place_import, "itinerary": itinerary,
    }
