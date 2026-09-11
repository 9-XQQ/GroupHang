"""Phase 3：分享文本解析与地点消歧。"""
import asyncio

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import User
from ..schemas import SharedTextParseRequest
from ..security import get_current_user
from ..services.access import require_trip_member
from ..services.amap import amap
from ..services.llm_parser import llm_place_parser
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
    llm_candidates = []
    llm_used = False
    llm_error = None
    if body.use_llm:
        llm_candidates, llm_error = await llm_place_parser.parse(body.text)
        llm_used = bool(llm_candidates)

    async def resolve_link(url: str) -> tuple[dict | None, str | None]:
        place = await amap.resolve_amap_share_url(url)
        return place, None if place else (amap.last_error or "无法解析高德分享链接")

    amap_urls = [url for url in result["source_urls"] if amap.is_amap_url(url)][:10]
    link_results = await asyncio.gather(*(resolve_link(url) for url in amap_urls))
    resolved = []
    warnings = list(result["warnings"])
    if body.use_llm and llm_error:
        warnings.append(f"AI 增强未生效，已使用规则解析：{llm_error}")
    for (place, error), url in zip(link_results, amap_urls):
        if not place:
            warnings.append(f"高德链接解析失败：{error}（{url[:100]}）")
            continue
        resolved.append({
            "name": place["name"], "address": place["address"], "category": "",
            "price": "", "reason": "来自高德地图分享链接",
            "expected_stay_min": body.default_stay_min, "source_url": url,
            "lat": place["lat"], "lng": place["lng"],
            "formatted_address": place["address"], "resolved": True,
            "resolution_error": None, "resolution_source": "amap_share_url",
        })

    text_candidates = list(result["candidates"])
    for candidate in llm_candidates:
        text_candidates.append({
            **candidate,
            "expected_stay_min": body.default_stay_min,
            "source_url": result["source_urls"][0] if result["source_urls"] else None,
        })

    for candidate in text_candidates:
        query = candidate["address"] or candidate["name"]
        location = await amap.geocode(query)
        item = {
            **candidate,
            "lat": location["lat"] if location else None,
            "lng": location["lng"] if location else None,
            "formatted_address": location["formatted"] if location else None,
            "resolved": bool(location),
            "resolution_error": None if location else (amap.last_error or "无法确定地点坐标，请补充详细地址"),
            "resolution_source": "geocode" if location else None,
        }
        duplicate = location and any(
            abs(float(existing["lat"]) - float(location["lat"])) < 0.00001
            and abs(float(existing["lng"]) - float(location["lng"])) < 0.00001
            for existing in resolved if existing.get("lat") is not None and existing.get("lng") is not None
        )
        if not duplicate:
            resolved.append(item)

    if resolved:
        warnings = [warning for warning in warnings if not warning.startswith("没有识别到地点")]
    parser = "rules_v1+amap_share_v1" + ("+llm_v1" if llm_used else "")
    return {
        **result, "candidates": resolved[:10], "warnings": warnings, "parser": parser,
        "llm_requested": body.use_llm, "llm_used": llm_used,
    }
