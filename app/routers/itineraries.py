"""Phase 2B：规划设置与多人多地点路线。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import ItineraryPlan, Trip, TripDestination, TripParticipant, User
from ..schemas import PlanningSettingsUpdate
from ..security import get_current_user
from ..services.access import require_trip_member
from ..services.itinerary import plan_itinerary
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
    }


@router.put("/planning-settings")
async def update_planning_settings(
    trip_id: int, body: PlanningSettingsUpdate,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    participant = await require_trip_member(db, trip_id, user)
    if participant.role != "creator":
        raise HTTPException(status_code=403, detail="只有发起人可以修改规划参数")
    trip = await db.get(Trip, trip_id)
    trip.planned_start_at = body.planned_start_at
    trip.planned_end_at = body.planned_end_at
    trip.group_transport_mode = body.group_transport_mode
    trip.optimization_objective = body.optimization_objective
    trip.input_version = (trip.input_version or 0) + 1
    await db.commit()
    await manager.broadcast(trip_id, {"type": "planning_settings_updated", "trip_id": trip_id, "input_version": trip.input_version})
    return {
        "planned_start_at": trip.planned_start_at, "planned_end_at": trip.planned_end_at,
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
        "group_transport_mode": trip.group_transport_mode,
        "optimization_objective": trip.optimization_objective, "input_version": trip.input_version,
    }


@router.post("/itinerary-plans")
async def create_itinerary_plan(
    trip_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    await require_trip_member(db, trip_id, user)
    trip = await db.get(Trip, trip_id)
    if not trip.planned_start_at or not trip.planned_end_at:
        raise HTTPException(status_code=409, detail="请先设置行程开始和结束时间")

    part_result = await db.execute(select(TripParticipant).where(TripParticipant.trip_id == trip_id))
    part_rows = list(part_result.scalars().all())
    if not part_rows:
        raise HTTPException(status_code=409, detail="至少需要 1 名参与者")
    participants = []
    missing = []
    for part in part_rows:
        person = await db.get(User, part.user_id)
        if not part.start_location:
            missing.append(person.name if person else str(part.user_id))
            continue
        participants.append({
            "user_id": part.user_id, "name": person.name if person else "未知", "mode": part.transport_mode,
            "lat": float(part.start_location["lat"]), "lng": float(part.start_location["lng"]),
            "available_from": part.available_from, "available_until": part.available_until,
        })
    if missing:
        raise HTTPException(status_code=409, detail=f"以下参与者尚未填写出发点：{missing}")

    dest_result = await db.execute(
        select(TripDestination).where(
            TripDestination.trip_id == trip_id,
            TripDestination.visit_status.in_(["must_visit", "optional"]),
        )
    )
    rows = list(dest_result.scalars().all())
    if len(rows) < 2:
        raise HTTPException(status_code=409, detail="请至少确认 2 个必去或可选地点")
    if len(rows) > 10:
        raise HTTPException(status_code=409, detail="MVP 最多支持 10 个确认地点")
    destinations = [{
        "id": row.id, "name": row.name, "lat": row.lat, "lng": row.lng,
        "expected_stay_min": row.expected_stay_min, "visit_status": row.visit_status,
        "opening_hours": row.opening_hours,
    } for row in rows]
    result = await plan_itinerary(
        participants, destinations, trip.planned_start_at, trip.planned_end_at,
        trip.group_transport_mode, trip.optimization_objective,
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
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    participant = await require_trip_member(db, trip_id, user)
    if participant.role != "creator":
        raise HTTPException(status_code=403, detail="只有发起人可以确认最终方案")
    trip = await db.get(Trip, trip_id)
    plan = await db.get(ItineraryPlan, plan_id)
    if plan is None or plan.trip_id != trip_id:
        raise HTTPException(status_code=404, detail="路线方案不存在")
    if plan.input_version != trip.input_version:
        raise HTTPException(status_code=409, detail="地点或规划参数已变化，请重新生成路线")
    plan.status = "confirmed"
    await db.commit()
    await db.refresh(plan)
    await manager.broadcast(trip_id, {"type": "itinerary_confirmed", "trip_id": trip_id, "plan_id": plan.id})
    return _plan_dict(plan, trip)
