"""Phase 5：读取 Trip 上下文、生成受控草案并在确认后执行。"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import AgentConversationState, AgentProposal, ItineraryPlan, Trip, TripDestination, TripParticipant, User
from ..schemas import AgentChatRequest, AgentProposalExecuteRequest, PlanningSettingsUpdate, SharedTextParseRequest
from ..security import get_current_user
from ..services.access import require_trip_active, require_trip_member
from ..services.agent_assistant import AgentAction, agent_assistant
from ..services.itinerary import plan_itinerary
from ..services.llm_parser import llm_place_parser
from ..services.ws import manager
from .itineraries import create_itinerary_plan
from .shared_text import parse_text

router = APIRouter(prefix="/trips/{trip_id}/assistant", tags=["assistant"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _store_proposal(
    db: AsyncSession, trip_id: int, user_id: int, input_version: int, actions: list[AgentAction],
) -> str | None:
    if not actions:
        return None
    order = {
        "switch_workflow": 0, "update_planning_settings": 1, "update_destination": 2,
        "prepare_place_import": 3, "compare_itineraries": 4, "generate_itinerary": 5,
    }
    unique = {action.type: action for action in actions}
    ordered = sorted(unique.values(), key=lambda action: order[action.type])
    proposal_id = str(uuid4())
    db.add(AgentProposal(
        id=proposal_id, trip_id=trip_id, user_id=user_id, input_version=input_version,
        actions=[action.model_dump(mode="json") for action in ordered],
        status="pending", expires_at=_now() + timedelta(minutes=10),
    ))
    await db.commit()
    return proposal_id


async def _save_structured_state(
    db: AsyncSession, trip_id: int, user_id: int, actions: list[AgentAction],
) -> None:
    summaries = [{
        key: value for key, value in action.model_dump(mode="json").items()
        if value is not None and key != "import_text"
    } for action in actions]
    existing = (await db.execute(select(AgentConversationState).where(
        AgentConversationState.trip_id == trip_id,
        AgentConversationState.user_id == user_id,
    ))).scalar_one_or_none()
    if existing:
        existing.last_actions = summaries
        existing.expires_at = _now() + timedelta(minutes=30)
    else:
        db.add(AgentConversationState(
            trip_id=trip_id, user_id=user_id, last_actions=summaries,
            expires_at=_now() + timedelta(minutes=30),
        ))
    await db.commit()


async def _claim_proposal(db: AsyncSession, proposal_id: str, trip_id: int, user_id: int) -> AgentProposal | None:
    result = await db.execute(
        update(AgentProposal)
        .where(
            AgentProposal.id == proposal_id, AgentProposal.trip_id == trip_id,
            AgentProposal.user_id == user_id, AgentProposal.status == "pending",
            AgentProposal.expires_at > _now(),
        )
        .values(status="executing", updated_at=_now())
        .returning(AgentProposal)
    )
    proposal = result.scalar_one_or_none()
    await db.commit()
    return proposal


async def _compare_itineraries(db: AsyncSession, trip: Trip) -> dict:
    """使用共享路线缓存模拟三种优化目标，不写入 itinerary_plans。"""
    if trip.primary_workflow != "itinerary":
        raise HTTPException(status_code=409, detail="请先把 Trip 主方案切换为多地点路线")
    if not trip.planned_start_at or not trip.planned_end_at:
        raise HTTPException(status_code=409, detail="请先设置行程开始和结束时间")
    participant_rows = list((await db.execute(
        select(TripParticipant).where(TripParticipant.trip_id == trip.id)
    )).scalars().all())
    participants = []
    missing = []
    for participant in participant_rows:
        participant_user = await db.get(User, participant.user_id)
        if not participant.start_location:
            missing.append(participant_user.name if participant_user else str(participant.user_id))
            continue
        participants.append({
            "user_id": participant.user_id,
            "name": participant_user.name if participant_user else "未知",
            "mode": participant.transport_mode,
            "lat": float(participant.start_location["lat"]),
            "lng": float(participant.start_location["lng"]),
            "available_from": participant.available_from,
            "available_until": participant.available_until,
        })
    if missing:
        raise HTTPException(status_code=409, detail=f"以下参与者尚未填写出发点：{missing}")
    destination_rows = list((await db.execute(
        select(TripDestination).where(
            TripDestination.trip_id == trip.id,
            TripDestination.visit_status.in_(["must_visit", "optional"]),
        )
    )).scalars().all())
    if not 2 <= len(destination_rows) <= 10:
        raise HTTPException(status_code=409, detail="路线比较需要 2～10 个已确认地点")
    destinations = [{
        "id": item.id, "name": item.name, "lat": item.lat, "lng": item.lng,
        "expected_stay_min": item.expected_stay_min, "visit_status": item.visit_status,
        "opening_hours": item.opening_hours,
        "visit_window_start": item.visit_window_start, "visit_window_end": item.visit_window_end,
    } for item in destination_rows]
    shared_cache = {}
    comparisons = []
    for objective in ("balanced", "total_time", "distance"):
        result = await plan_itinerary(
            participants, destinations, trip.planned_start_at, trip.planned_end_at,
            trip.group_transport_mode, objective, shared_route_cache=shared_cache,
        )
        warnings = result.get("warnings") or []
        estimated_count = sum(1 for item in [
            *(result.get("participant_arrivals") or []), *(result.get("route_legs") or []),
        ] if item.get("estimated"))
        comparisons.append({
            "objective": objective,
            "ordered_stops": [{
                "destination_id": item["destination_id"], "name": item["name"],
                "order_index": item["order_index"], "arrival_at": item["arrival_at"],
                "leave_at": item["leave_at"], "wait_min": item.get("wait_min", 0),
            } for item in result["ordered_stops"]],
            "total_travel_min": result["total_travel_min"],
            "total_duration_min": result["total_duration_min"],
            "total_distance_km": result["total_distance_km"],
            "total_wait_min": sum(int(item.get("wait_min") or 0) for item in result["ordered_stops"]),
            "estimated_segment_count": estimated_count,
            "time_window_conflict_count": sum(
                1 for item in warnings if item.get("code") == "destination_time_window_conflict"
            ),
            "warning_codes": sorted({item.get("code") for item in warnings if item.get("code")}),
        })
    return {"saved": False, "input_version": trip.input_version, "comparisons": comparisons}


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
    structured_state = (await db.execute(select(AgentConversationState).where(
        AgentConversationState.trip_id == trip.id,
        AgentConversationState.user_id == user.id,
        AgentConversationState.expires_at > _now(),
    ))).scalar_one_or_none()
    destinations_context = [{
        "id": item.id, "name": item.name, "address": item.address, "category": item.category,
        "visit_status": item.visit_status, "expected_stay_min": item.expected_stay_min,
        "opening_hours": item.opening_hours,
        "visit_window_start": item.visit_window_start, "visit_window_end": item.visit_window_end,
    } for item in destinations]
    raw_stops = (latest_plan.ordered_stops or []) if latest_plan else []
    raw_warnings = (latest_plan.warnings or []) if latest_plan else []
    plan_context = None if latest_plan is None else {
        "status": latest_plan.status,
        "is_stale": latest_plan.input_version != trip.input_version,
        "ordered_stops": [{
            "destination_id": item.get("destination_id"), "name": item.get("name"),
            "order_index": item.get("order_index"), "arrival_at": item.get("arrival_at"),
            "leave_at": item.get("leave_at"), "wait_min": item.get("wait_min", 0),
            "visit_window_start": item.get("visit_window_start"),
            "visit_window_end": item.get("visit_window_end"),
        } for item in raw_stops],
        "warnings": [{
            "code": item.get("code"), "message": item.get("message"),
            "destination_id": item.get("destination_id"), "user_id": item.get("user_id"),
            "wait_min": item.get("wait_min"), "late_min": item.get("late_min"),
        } for item in raw_warnings],
        "total_travel_min": latest_plan.total_travel_min,
        "total_duration_min": latest_plan.total_duration_min,
    }
    participant_context = [{
        "name": participant_user.name, "role": participant.role,
        "transport_mode": participant.transport_mode,
        "has_start_location": bool(participant.start_location),
        "available_from": participant.available_from, "available_until": participant.available_until,
    } for participant, participant_user in participant_rows]
    warnings = raw_warnings
    participant_routes = (latest_plan.participant_arrivals or []) if latest_plan else []
    route_legs = (latest_plan.route_legs or []) if latest_plan else []
    stops = raw_stops
    estimated_items = [item for item in [*participant_routes, *route_legs] if item.get("estimated")]
    route_summary = {
        "has_plan": latest_plan is not None,
        "is_stale": bool(latest_plan and latest_plan.input_version != trip.input_version),
        "real_segment_count": len(participant_routes) + len(route_legs) - len(estimated_items),
        "estimated_segment_count": len(estimated_items),
        "total_wait_min": sum(int(item.get("wait_min") or 0) for item in stops),
        "time_window_conflict_count": sum(
            1 for item in warnings if item.get("code") == "destination_time_window_conflict"
        ),
        "warning_codes": sorted({item.get("code") for item in warnings if item.get("code")}),
    }
    context = {
        "trip": {
            "title": trip.title, "status": trip.status, "primary_workflow": trip.primary_workflow,
            "planned_start_at": trip.planned_start_at, "planned_end_at": trip.planned_end_at,
            "group_transport_mode": trip.group_transport_mode,
            "optimization_objective": trip.optimization_objective,
            "input_version": trip.input_version,
        },
        "participants": participant_context,
        "destinations": destinations_context,
        "latest_plan": plan_context,
        "route_summary": route_summary,
        "recent_action_state": structured_state.last_actions if structured_state else [],
        "my_preferences": {
            "personalization_enabled": bool((user.preferences or {}).get("personalization_enabled", True)),
            "explicit": (user.preferences or {}).get("explicit", {}),
            "derived": (user.preferences or {}).get("derived", {}),
        },
    }
    context["planning_readiness"] = agent_assistant.planning_readiness(context)
    return context


@router.post("/chat")
async def chat(
    trip_id: int, body: AgentChatRequest,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    participant = await require_trip_member(db, trip_id, user)
    trip = await db.get(Trip, trip_id)
    context = await _context(db, trip, user)
    answer, llm_used, warning = await agent_assistant.chat(
        context, body.message, trip_id, user.id,
    )
    actions = answer.actions if participant.role == "creator" and trip.status == "active" else []
    proposal_id = await _store_proposal(db, trip_id, user.id, trip.input_version, actions)
    await _save_structured_state(db, trip_id, user.id, actions)
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
        "planning_readiness": context["planning_readiness"],
        "route_summary": context["route_summary"],
    }


@router.delete("/history")
async def clear_history(
    trip_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    await require_trip_member(db, trip_id, user)
    agent_assistant.clear_history(trip_id, user.id)
    await db.execute(delete(AgentConversationState).where(
        AgentConversationState.trip_id == trip_id,
        AgentConversationState.user_id == user.id,
    ))
    await db.commit()
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
    proposal_id = str(body.proposal_id)
    proposal = await _claim_proposal(db, proposal_id, trip_id, user.id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="草案不存在、已过期或正在执行，请重新询问助手")
    if proposal.input_version != trip.input_version:
        proposal.status = "expired"
        await db.commit()
        raise HTTPException(status_code=409, detail="Trip 已发生变化，请重新询问助手后再确认")

    executed = []
    place_import = None
    itinerary = None
    comparison = None
    try:
        for raw_action in proposal.actions:
            action = AgentAction.model_validate(raw_action)
            if action.type == "switch_workflow":
                trip.primary_workflow = action.primary_workflow
                executed.append({"type": action.type, "status": "completed", "primary_workflow": action.primary_workflow})
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
                executed.append({"type": action.type, "status": "completed"})
            elif action.type == "prepare_place_import":
                place_import = await parse_text(
                    trip_id,
                    SharedTextParseRequest(text=action.import_text, use_llm=True),
                    user,
                    db,
                )
                executed.append({
                    "type": action.type, "status": "completed",
                    "candidate_count": len(place_import.get("candidates", [])),
                })
            elif action.type == "update_destination":
                destination = await db.get(TripDestination, action.destination_id)
                if destination is None or destination.trip_id != trip_id:
                    raise HTTPException(status_code=404, detail="助手草案中的地点不存在")
                if action.expected_stay_min is not None:
                    destination.expected_stay_min = action.expected_stay_min
                if action.visit_status is not None:
                    destination.visit_status = action.visit_status
                if action.visit_window_start is not None:
                    destination.visit_window_start = action.visit_window_start
                    destination.visit_window_end = action.visit_window_end
                trip.input_version = (trip.input_version or 0) + 1
                executed.append({
                    "type": action.type, "status": "completed", "destination_id": destination.id,
                })
            elif action.type == "generate_itinerary":
                itinerary = await create_itinerary_plan(trip_id, user, db)
                executed.append({
                    "type": action.type, "status": "completed", "plan_id": itinerary.get("plan_id"),
                })
            elif action.type == "compare_itineraries":
                comparison = await _compare_itineraries(db, trip)
                executed.append({
                    "type": action.type, "status": "completed",
                    "variant_count": len(comparison["comparisons"]),
                })
        await db.commit()
    except Exception:
        await db.rollback()
        await db.execute(
            update(AgentProposal)
            .where(AgentProposal.id == proposal_id, AgentProposal.status == "executing")
            .values(status="pending", updated_at=_now())
        )
        await db.commit()
        raise
    proposal = await db.get(AgentProposal, proposal_id)
    if proposal:
        proposal.status = "consumed"
        await db.commit()
    await manager.broadcast(trip_id, {
        "type": "assistant_actions_executed", "trip_id": trip_id,
        "input_version": trip.input_version, "primary_workflow": trip.primary_workflow,
    })
    return {
        "trip_id": trip_id, "executed": executed, "input_version": trip.input_version,
        "place_import": place_import, "itinerary": itinerary, "comparison": comparison,
    }
