"""Phase 3：分享文本解析与地点消歧。"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import User
from ..schemas import SharedTextParseRequest
from ..security import get_current_user
from ..services.access import require_trip_member
from ..services.amap import amap
from ..services.shared_text import parse_shared_text

router = APIRouter(prefix="/trips/{trip_id}/shared-text", tags=["shared-text"])


@router.post("/parse")
async def parse_text(
    trip_id: int,
    body: SharedTextParseRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_trip_member(db, trip_id, user)
    result = parse_shared_text(body.text, body.default_stay_min)
    resolved = []
    for candidate in result["candidates"]:
        query = candidate["address"] or candidate["name"]
        location = await amap.geocode(query)
        resolved.append({
            **candidate,
            "lat": location["lat"] if location else None,
            "lng": location["lng"] if location else None,
            "formatted_address": location["formatted"] if location else None,
            "resolved": bool(location),
            "resolution_error": None if location else (amap.last_error or "无法确定地点坐标，请补充详细地址"),
        })
    return {**result, "candidates": resolved, "parser": "rules_v1"}
