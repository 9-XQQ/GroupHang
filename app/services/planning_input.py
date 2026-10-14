"""Detached planning inputs shared by HTTP planning and Agent previews."""
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Trip, TripDestination, TripParticipant, User


@dataclass
class PlanningInput:
    workflow: str
    start: datetime | None
    end: datetime | None
    mode: str
    objective: str
    participants: list[dict]
    destinations: list[dict]
    return_location: dict | None = None

    def validate(self) -> tuple[list[dict], list[dict]]:
        if self.workflow != "itinerary":
            raise HTTPException(status_code=409, detail="请先把 Trip 主方案切换为多地点路线")
        if not self.start or not self.end:
            raise HTTPException(status_code=409, detail="请先设置行程开始和结束时间")
        if not self.participants:
            raise HTTPException(status_code=409, detail="至少需要 1 名参与者")
        missing = [p["name"] for p in self.participants if p.get("lat") is None]
        if missing:
            raise HTTPException(status_code=409, detail=f"以下参与者尚未填写出发点：{missing}")
        destinations = [d for d in self.destinations if d["visit_status"] in {"must_visit", "optional"}]
        if not 2 <= len(destinations) <= 10:
            raise HTTPException(status_code=409, detail="路线规划需要 2～10 个已确认地点")
        return self.participants, destinations


async def load_planning_input(db: AsyncSession, trip: Trip) -> PlanningInput:
    rows = (await db.execute(select(TripParticipant, User).join(
        User, User.id == TripParticipant.user_id,
    ).where(TripParticipant.trip_id == trip.id))).all()
    participants = [{
        "user_id": p.user_id, "name": u.name, "mode": p.transport_mode,
        "lat": float(p.start_location["lat"]) if p.start_location else None,
        "lng": float(p.start_location["lng"]) if p.start_location else None,
        "available_from": p.available_from, "available_until": p.available_until,
    } for p, u in rows]
    destinations = (await db.execute(select(TripDestination).where(
        TripDestination.trip_id == trip.id,
    ).order_by(TripDestination.id))).scalars().all()
    return PlanningInput(
        trip.primary_workflow, trip.planned_start_at, trip.planned_end_at,
        trip.group_transport_mode, trip.optimization_objective, participants,
        [{key: getattr(d, key) for key in (
            "id", "name", "lat", "lng", "expected_stay_min", "visit_status",
            "opening_hours", "visit_window_start", "visit_window_end",
        )} for d in destinations], trip.return_location,
    )
