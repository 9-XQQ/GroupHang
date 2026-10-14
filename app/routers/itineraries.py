"""Phase 2B：规划设置与多人多地点路线。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import ItineraryPlan, Trip, User
from ..schemas import ConfirmPlanRequest, PlanningSettingsUpdate
from ..security import get_current_user
from ..services.access import lock_trip, lock_trip_active, require_trip_member
from ..services.itinerary import plan_itinerary
from ..services.planning_input import load_planning_input
from ..services.ws import manager

router = APIRouter(prefix="/trips/{trip_id}", tags=["itineraries"])


def _plan_dict(plan: ItineraryPlan, trip: Trip) -> dict:
    return {
        "plan_id": plan.id, "trip_id": plan.trip_id, "input_version": plan.input_version,
        "is_stale": plan.input_version != trip.input_version, "objective": plan.objective, "status": plan.status,
        "first_destination_id": plan.first_destination_id,
        "participant_arrivals": plan.participant_arrivals, "ordered_stops": plan.ordered_stops,
        "route_legs": plan.route_legs, "warnings": plan.warnings,
        "total_travel_min": plan.total_travel_min, "total_duration_min": plan.total_duration_min,
        "computed_at": plan.computed_at,
        "return_leg": next((leg for leg in (plan.route_legs or []) if leg.get("kind") == "return"), None),
        "finish_at": next((leg["arrival_at"] for leg in (plan.route_legs or []) if leg.get("kind") == "return"),
                          plan.ordered_stops[-1]["leave_at"] if plan.ordered_stops else None),
    }


@router.put("/planning-settings")
async def update_planning_settings(
    trip_id: int, body: PlanningSettingsUpdate,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    participant = await require_trip_member(db, trip_id, user)
    if participant.role != "creator":
        raise HTTPException(status_code=403, detail="只有发起人可以修改规划参数")
    trip = await lock_trip_active(db, trip_id)
    if trip.primary_workflow != "itinerary":
        raise HTTPException(status_code=409, detail="当前 Trip 主方案是共同约点，请由发起人先切换为多地点路线")
    trip.planned_start_at = body.planned_start_at
    trip.planned_end_at = body.planned_end_at
    trip.group_transport_mode = body.group_transport_mode
    trip.optimization_objective = body.optimization_objective
    if "return_location" in body.model_fields_set:
        trip.return_location = body.return_location.model_dump() if body.return_location else None
    trip.input_version = (trip.input_version or 0) + 1
    await db.commit()
    await manager.broadcast(trip_id, {"type": "planning_settings_updated", "trip_id": trip_id, "input_version": trip.input_version})
    return {
        "planned_start_at": trip.planned_start_at, "planned_end_at": trip.planned_end_at,
        "return_location": trip.return_location,
        "group_transport_mode": trip.group_transport_mode,
        "optimization_objective": trip.optimization_objective, "input_version": trip.input_version,
    }


@router.get("/planning-settings")
async def get_planning_settings(
    trip_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    await require_trip_member(db, trip_id, user)
    trip = await db.get(Trip, trip_id)
    return {
        "planned_start_at": trip.planned_start_at, "planned_end_at": trip.planned_end_at,
        "return_location": trip.return_location,
        "group_transport_mode": trip.group_transport_mode,
        "optimization_objective": trip.optimization_objective, "input_version": trip.input_version,
    }


@router.post("/itinerary-plans")
async def create_itinerary_plan(
    trip_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    await require_trip_member(db, trip_id, user)
    trip = await lock_trip_active(db, trip_id)
    if trip.primary_workflow != "itinerary":
        raise HTTPException(status_code=409, detail="当前 Trip 主方案是共同约点，请由发起人先切换为多地点路线")
    if not trip.planned_start_at or not trip.planned_end_at:
        raise HTTPException(status_code=409, detail="请先设置行程开始和结束时间")

    inputs = await load_planning_input(db, trip)
    participants, destinations = inputs.validate()
    result = await plan_itinerary(
        participants, destinations, trip.planned_start_at, trip.planned_end_at,
        trip.group_transport_mode, trip.optimization_objective,
        **({"return_location": inputs.return_location} if inputs.return_location else {}),
    )
    plan = ItineraryPlan(
        trip_id=trip_id, input_version=trip.input_version, objective=trip.optimization_objective,
        first_destination_id=result["first_destination_id"],
        participant_arrivals=result["participant_arrivals"], ordered_stops=result["ordered_stops"],
        route_legs=result["route_legs"], warnings=result["warnings"],
        total_travel_min=result["total_travel_min"], total_duration_min=result["total_duration_min"],
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    await manager.broadcast(trip_id, {"type": "itinerary_ready", "trip_id": trip_id, "plan_id": plan.id})
    response = _plan_dict(plan, trip)
    response["group_start_at"] = result["group_start_at"]
    response["total_distance_km"] = result["total_distance_km"]
    return response


@router.get("/itinerary-plans/latest")
async def get_latest_plan(
    trip_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    await require_trip_member(db, trip_id, user)
    trip = await db.get(Trip, trip_id)
    result = await db.execute(
        select(ItineraryPlan).where(ItineraryPlan.trip_id == trip_id).order_by(ItineraryPlan.computed_at.desc(), ItineraryPlan.id.desc()).limit(1)
    )
    plan = result.scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=404, detail="尚未生成路线方案")
    return _plan_dict(plan, trip)


@router.post("/itinerary-plans/{plan_id}/confirm")
async def confirm_plan(
    trip_id: int, plan_id: int,
    body: ConfirmPlanRequest | None = None,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    participant = await require_trip_member(db, trip_id, user)
    if participant.role != "creator":
        raise HTTPException(status_code=403, detail="只有发起人可以确认最终方案")
    trip = await lock_trip(db, trip_id)
    plan = await db.get(ItineraryPlan, plan_id)
    if plan is None or plan.trip_id != trip_id:
        raise HTTPException(status_code=404, detail="路线方案不存在")
    if plan.input_version != trip.input_version:
        raise HTTPException(status_code=409, detail="地点或规划参数已变化，请重新生成路线")
    if trip.status != "active":
        raise HTTPException(status_code=409, detail="trip 已确认锁定")
    has_estimated = any(item.get("estimated") for item in (plan.participant_arrivals or [])) or any(
        item.get("estimated") for item in (plan.route_legs or [])
    )
    if has_estimated and not (body and body.accept_estimated_routes):
        raise HTTPException(
            status_code=409,
            detail="方案包含不可导航的直线估算路段；请恢复高德配额后重新规划，或明确接受估算结果",
        )
    plan.status = "confirmed"
    trip.status = "confirmed"
    await db.commit()
    await db.refresh(plan)
    await manager.broadcast(trip_id, {"type": "itinerary_confirmed", "trip_id": trip_id, "plan_id": plan.id})
    return _plan_dict(plan, trip)
