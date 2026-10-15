"""Read-only discovery tools used by Agent chat and scenario controls."""
import asyncio

from ..services.agent_trace import traced_stage
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Trip, User
from ..schemas import DiscoveryRequest
from ..security import get_current_user
from ..services.access import require_trip_member
from ..services.place_discovery import discover

router = APIRouter(tags=["assistant-discovery"])


@traced_stage("discovery.recommend")
async def run_discovery(trip_id, body, user, db):
    await require_trip_member(db, trip_id, user)
    trip = await db.get(Trip, trip_id)
    version = trip.input_version
    try:
        async with asyncio.timeout(75):
            result = await discover(db, trip, user, body)
    except TimeoutError:
        await db.rollback()
        raise HTTPException(504, "推荐计算超时，请缩小范围或使用收藏地点重试") from None
    await db.commit()
    current = await db.get(Trip, trip_id, populate_existing=True)
    if current is None or current.input_version != version:
        raise HTTPException(409, "推荐期间行程发生变化，请重新计算")
    result.update({"reply": "请补充：" + "；".join(result["needs"]) if result.get("needs") else
                  f"已按路线工具评估{len(result.get('recommendations', []))}个推荐，请查看耗时和说明后确认加入。",
                   "llm_used": False, "proposal_id": None, "actions": []})
    return result


@router.post("/trips/{trip_id}/assistant/discover")
async def discover_places(trip_id: int, body: DiscoveryRequest,
                          user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await run_discovery(trip_id, body, user, db)
