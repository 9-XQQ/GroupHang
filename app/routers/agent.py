"""Phase 5：读取 Trip 上下文、生成受控草案并在确认后执行。"""
from ..services.agent_trace import read_traces, traced_request, traced_stage
from datetime import datetime, timedelta, timezone
from copy import deepcopy

from fastapi.encoders import jsonable_encoder
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import AgentConversationState, AgentProposal, ItineraryPlan, Trip, TripDestination, TripParticipant, User
from ..schemas import AgentChatRequest, AgentProposalExecuteRequest, PlanningSettingsUpdate, SharedTextParseRequest
from ..security import get_current_user
from ..services.access import lock_trip, lock_trip_active, require_trip_member
from ..services.agent_assistant import AgentAction, agent_assistant, normalize_actions
from ..services.agent_state import PROPOSAL_TTL, claim_proposal, history_state, maintain_lease, release_proposal
from ..services.planning_input import PlanningInput, load_planning_input
from ..services.itinerary import plan_itinerary
from ..services.llm_parser import llm_place_parser
from ..services.ws import manager
from .itineraries import _plan_dict
from .shared_text import parse_text
from .discovery import run_discovery
from ..schemas import DiscoveryRequest
from ..services.place_discovery import scene_from_message

router = APIRouter(prefix="/trips/{trip_id}/assistant", tags=["assistant"])


@router.get("/traces")
async def list_traces(trip_id: int, user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)) -> dict:
    await require_trip_member(db, trip_id, user)
    return {"storage": "process_local", "retention_seconds": 86400,
            "traces": read_traces(user.id, trip_id)}


@router.get("/traces/{trace_id}")
async def get_trace(trip_id: int, trace_id: str, user: User = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)) -> dict:
    await require_trip_member(db, trip_id, user)
    traces = read_traces(user.id, trip_id, trace_id)
    if not traces:
        raise HTTPException(status_code=404, detail="Trace not found")
    return traces[0]


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _store_proposal(
    db: AsyncSession, trip_id: int, user_id: int, input_version: int, actions: list[AgentAction],
) -> str | None:
    if not actions:
        return None
    proposal_id = str(uuid4())
    db.add(AgentProposal(
        id=proposal_id, trip_id=trip_id, user_id=user_id, input_version=input_version,
        actions=[action.model_dump(mode="json") for action in normalize_actions(actions)],
        status="pending", expires_at=func.clock_timestamp() + PROPOSAL_TTL,
    ))
    return proposal_id


async def _compare_inputs(inputs: PlanningInput, input_version: int) -> dict:
    participants, destinations = inputs.validate()
    shared_cache = {}
    comparisons = []
    for objective in ("balanced", "total_time", "distance"):
        result = await plan_itinerary(
            participants, destinations, inputs.start, inputs.end,
            inputs.mode, objective, shared_route_cache=shared_cache,
            **({"return_location": inputs.return_location} if inputs.return_location else {}),
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
            "finish_at": result.get("finish_at"), "has_return_leg": bool(result.get("return_leg")),
            "total_distance_km": result["total_distance_km"],
            "total_wait_min": sum(int(item.get("wait_min") or 0) for item in result["ordered_stops"]),
            "estimated_segment_count": estimated_count,
            "time_window_conflict_count": sum(
                1 for item in warnings if item.get("code") == "destination_time_window_conflict"
            ),
            "warning_codes": sorted({item.get("code") for item in warnings if item.get("code")}),
        })
    return {"saved": False, "input_version": input_version, "comparisons": comparisons}


@traced_stage("context.load")
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
            "has_return_location": bool(trip.return_location),
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
@traced_request("chat")
async def chat(
    trip_id: int, body: AgentChatRequest,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    if body.discovery or scene_from_message(body.message):
        request = body.discovery or DiscoveryRequest(message=body.message)
        request = request.model_copy(update={"message": body.message})
        result = await run_discovery(trip_id, request, user, db)
        return {"reply": result["reply"], "actions": [], "proposal_id": None,
                "llm_used": False, "discovery": result}
    participant = await require_trip_member(db, trip_id, user)
    trip = await lock_trip(db, trip_id)
    state = await history_state(db, trip_id, user.id)
    epoch, version = state.history_epoch, trip.input_version
    context = await _context(db, trip, user)
    await db.commit()
    agent_assistant.sync_history_epoch(trip_id, user.id, epoch)
    answer, llm_used, warning = await agent_assistant.chat(context, body.message, trip_id, user.id)

    # Revalidate after the external call: clear-history or Trip edits may have
    # happened while the model was responding. Trip is always the first lock.
    trip = await lock_trip(db, trip_id)
    state = await history_state(db, trip_id, user.id)
    actions = normalize_actions(answer.actions) if (
        participant.role == "creator" and trip.status == "active"
        and trip.input_version == version and state.history_epoch == epoch
    ) else []
    proposal_id = None
    if state.history_epoch != epoch:
        agent_assistant.clear_history(trip_id, user.id)
        warning = "历史已清空，本次回复不再保存草案；请重新提问"
    else:
        proposal_id = await _store_proposal(db, trip_id, user.id, version, actions)
        state.last_actions = [{
            key: value for key, value in action.model_dump(mode="json").items()
            if value is not None and key != "import_text"
        } for action in actions]
        state.expires_at = _now() + timedelta(minutes=30)
        if answer.actions and not actions:
            warning = "Trip 状态或版本已变化，或当前用户不是发起人；本次仅展示建议"
    await db.commit()
    return {
        "reply": answer.reply, "llm_used": llm_used,
        "llm_usage": llm_place_parser.last_usage, "warning": warning,
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
    await lock_trip(db, trip_id)
    state = await history_state(db, trip_id, user.id)
    state.history_epoch = str(uuid4())
    state.last_actions = []
    state.expires_at = _now()
    await db.execute(update(AgentProposal).where(
        AgentProposal.trip_id == trip_id, AgentProposal.user_id == user.id,
        AgentProposal.status.in_(["pending", "executing"]),
    ).values(status="cancelled", actions=[], lease_token=None, lease_until=None, updated_at=func.now()))
    await db.commit()
    agent_assistant.clear_history(trip_id, user.id)
    return {"cleared": True}


def _project_actions(inputs: PlanningInput, actions: list[AgentAction]) -> int:
    """Apply edits to detached inputs only. No writes or network requests."""
    bumps = 0
    for action in actions:
        if action.type == "switch_workflow":
            inputs.workflow = action.primary_workflow
            bumps += 1
        elif action.type == "update_planning_settings":
            if inputs.workflow != "itinerary":
                raise HTTPException(status_code=409, detail="请先切换为多地点路线")
            settings = PlanningSettingsUpdate(
                planned_start_at=action.planned_start_at, planned_end_at=action.planned_end_at,
                group_transport_mode=action.group_transport_mode or inputs.mode,
                optimization_objective=action.optimization_objective or inputs.objective,
            )
            inputs.start, inputs.end = settings.planned_start_at, settings.planned_end_at
            inputs.mode, inputs.objective = settings.group_transport_mode, settings.optimization_objective
            bumps += 1
        elif action.type == "update_destination":
            destination = next((d for d in inputs.destinations if d["id"] == action.destination_id), None)
            if destination is None:
                raise HTTPException(status_code=404, detail="助手草案中的地点不存在")
            for key in ("expected_stay_min", "visit_status", "visit_window_start", "visit_window_end"):
                if getattr(action, key) is not None:
                    destination[key] = getattr(action, key)
            if action.clear_visit_window:
                destination["visit_window_start"] = destination["visit_window_end"] = None
            bumps += 1
    return bumps


@traced_stage("execution.prepare")
async def _prepare_execution(
    trip_id: int, user: User, db: AsyncSession, inputs: PlanningInput,
    actions: list[AgentAction], version: int,
) -> tuple[dict | None, dict | None, dict | None]:
    place_import = comparison = route = None
    # No persistent writes until every external computation has succeeded.
    for action in actions:
        if action.type == "prepare_place_import":
            place_import = await parse_text(
                trip_id, SharedTextParseRequest(text=action.import_text, use_llm=True), user, db,
            )
        elif action.type == "compare_itineraries":
            comparison = await _compare_inputs(inputs, version)
        elif action.type == "generate_itinerary":
            participants, destinations = inputs.validate()
            route = await plan_itinerary(
                participants, destinations, inputs.start, inputs.end, inputs.mode, inputs.objective,
                **({"return_location": inputs.return_location} if inputs.return_location else {}),
            )
    return place_import, comparison, route


@traced_stage("execution.commit")
async def _commit_execution(
    db: AsyncSession, trip_id: int, user_id: int, proposal_id: str, token: str,
    version: int, inputs: PlanningInput, actions: list[AgentAction],
    bumps: int, place_import: dict | None, comparison: dict | None, route: dict | None,
) -> dict:
    trip = await lock_trip(db, trip_id)
    proposal = (await db.execute(select(AgentProposal).where(
        AgentProposal.id == proposal_id, AgentProposal.trip_id == trip_id,
        AgentProposal.user_id == user_id,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if proposal is None or proposal.status != "executing" or proposal.lease_token != token:
        raise HTTPException(status_code=409, detail="草案已撤销或已由其他请求接管")
    # Use database time after acquiring locks, rather than the transaction's
    # start time, so time spent waiting cannot extend the lease/retention window.
    clock = (await db.execute(select(func.clock_timestamp()))).scalar_one()
    if proposal.expires_at <= clock:
        proposal.status, proposal.actions = "expired", []
        proposal.lease_token = proposal.lease_until = None
        await db.commit()
        raise HTTPException(status_code=409, detail="草案已过期，请重新提问")
    if proposal.lease_until <= clock:
        raise HTTPException(status_code=409, detail="草案或执行租约已过期，请重试或重新提问")
    if trip.status != "active" or trip.input_version != version or trip.creator_id != user_id:
        proposal.status, proposal.actions = "expired", []
        proposal.lease_token = proposal.lease_until = None
        await db.commit()
        raise HTTPException(status_code=409, detail="Trip 已发生变化，请重新询问助手")
    executed = []
    for action in actions:
        item = {"type": action.type, "status": "completed"}
        if action.type == "switch_workflow":
            trip.primary_workflow = inputs.workflow
            item["primary_workflow"] = inputs.workflow
        elif action.type == "update_planning_settings":
            trip.planned_start_at, trip.planned_end_at = inputs.start, inputs.end
            trip.group_transport_mode, trip.optimization_objective = inputs.mode, inputs.objective
        elif action.type == "update_destination":
            destination = await db.get(TripDestination, action.destination_id)
            if destination is None or destination.trip_id != trip_id:
                raise HTTPException(status_code=409, detail="地点已变化，请重新提问")
            for key in ("expected_stay_min", "visit_status", "visit_window_start", "visit_window_end"):
                if getattr(action, key) is not None:
                    setattr(destination, key, getattr(action, key))
            if action.clear_visit_window:
                destination.visit_window_start = destination.visit_window_end = None
                item["cleared_visit_window"] = True
            item["destination_id"] = destination.id
        elif action.type == "prepare_place_import":
            item["candidate_count"] = len(place_import.get("candidates", []))
        elif action.type == "compare_itineraries":
            item["variant_count"] = len(comparison["comparisons"])
        executed.append(item)
    trip.input_version = version + bumps
    itinerary = None
    if route is not None:
        plan = ItineraryPlan(
            trip_id=trip_id, input_version=trip.input_version, objective=inputs.objective,
            **{key: route[key] for key in (
                "first_destination_id", "participant_arrivals", "ordered_stops", "route_legs",
                "warnings", "total_travel_min", "total_duration_min",
            )},
        )
        db.add(plan)
        await db.flush()
        await db.refresh(plan)
        itinerary = {**_plan_dict(plan, trip), "group_start_at": route["group_start_at"],
                     "total_distance_km": route["total_distance_km"]}
        for item in executed:
            if item["type"] == "generate_itinerary":
                item["plan_id"] = plan.id
    response = jsonable_encoder({
        "trip_id": trip_id, "executed": executed, "input_version": trip.input_version,
        "place_import": place_import, "itinerary": itinerary, "comparison": comparison,
    })
    # Do not persist parser output: candidate reasons/URLs/comment evidence can
    # contain original text. Receipts replay committed writes, never re-run them.
    receipt = deepcopy(response)
    receipt["place_import"] = None
    if place_import is not None:
        receipt["warning"] = "草案已执行；导入候选仅在首次响应中返回，请重新解析文本以获取候选"
    proposal.result, proposal.status, proposal.actions = receipt, "consumed", []
    proposal.lease_token = proposal.lease_until = None
    await db.commit()  # the only commit for business edits + plan + receipt
    return response


@router.post("/proposals/execute")
@traced_request("execute")
async def execute_proposal(
    trip_id: int, body: AgentProposalExecuteRequest,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    participant = await require_trip_member(db, trip_id, user)
    if participant.role != "creator":
        raise HTTPException(status_code=403, detail="只有发起人可以执行助手草案")
    user_id = user.id
    proposal_id = str(body.proposal_id)
    existing = (await db.execute(select(AgentProposal).where(
        AgentProposal.id == proposal_id, AgentProposal.trip_id == trip_id,
        AgentProposal.user_id == user_id,
    ).execution_options(populate_existing=True))).scalar_one_or_none()
    if existing and existing.status == "consumed" and existing.result is not None:
        return {**existing.result, "replayed": True}
    proposal = await claim_proposal(db, proposal_id, trip_id, user_id)
    if proposal is None:
        # A concurrent winner may have committed between the read and claim.
        await db.rollback()
        existing = (await db.execute(select(AgentProposal).where(
            AgentProposal.id == proposal_id, AgentProposal.trip_id == trip_id,
            AgentProposal.user_id == user_id,
        ))).scalar_one_or_none()
        if existing and existing.status == "consumed" and existing.result is not None:
            return {**existing.result, "replayed": True}
        raise HTTPException(status_code=409, detail="草案不存在、已过期、已撤销或正在执行")
    token, version = proposal.lease_token, proposal.input_version
    raw_actions = deepcopy(proposal.actions)
    await db.commit()
    try:
        trip = await lock_trip_active(db, trip_id)
        if trip.input_version != version:
            await db.execute(update(AgentProposal).where(
                AgentProposal.id == proposal_id, AgentProposal.lease_token == token,
            ).values(status="expired", actions=[], lease_token=None, lease_until=None))
            await db.commit()
            raise HTTPException(status_code=409, detail="Trip 已发生变化，请重新询问助手")
        inputs = await load_planning_input(db, trip)
        actions = normalize_actions([AgentAction.model_validate(action) for action in raw_actions])
        bumps = _project_actions(inputs, actions)
        await db.commit()  # release the snapshot lock before slow network work
        async with maintain_lease(db, proposal_id, token):
            prepared = await _prepare_execution(trip_id, user, db, inputs, actions, version + bumps)
        await db.rollback()  # end any read transaction opened by text parsing
        response = await _commit_execution(
            db, trip_id, user_id, proposal_id, token, version, inputs, actions, bumps, *prepared,
        )
    except Exception:
        await db.rollback()
        await release_proposal(db, proposal_id, token)
        await db.commit()
        raise
    await manager.broadcast(trip_id, {
        "type": "assistant_actions_executed", "trip_id": trip_id,
        "input_version": response["input_version"], "primary_workflow": inputs.workflow,
    })
    return response
