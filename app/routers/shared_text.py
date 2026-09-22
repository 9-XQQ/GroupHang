"""Phase 3：分享文本解析与地点消歧。"""
import asyncio
import re
from uuid import uuid4

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import User
from ..schemas import SharedTextParseRequest
from ..security import get_current_user
from ..services.access import require_trip_member
from ..services.amap import amap
from ..services.llm_parser import llm_place_parser
from ..services.shared_text import associate_comments_to_candidates, parse_shared_text, summarize_comments
from ..services.source_adapters import adapt_source_text

router = APIRouter(prefix="/trips/{trip_id}/shared-text", tags=["shared-text"])


def _infer_city(text: str) -> str:
    """只提取文本明确给出的城市上下文，不维护硬编码城市表。"""
    match = re.search(r"(?:在|到|去)([\u4e00-\u9fff]{2,8}?)(?:市)?(?:吃|玩|逛|旅行|旅游|出差)", text)
    return match.group(1).strip() if match else ""


def _normalized_place_name(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", value.lower())


@router.post("/parse")
async def parse_text(
    trip_id: int,
    body: SharedTextParseRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_trip_member(db, trip_id, user)
    imported = adapt_source_text(body.source_platform, body.text, body.comments_text)
    result = parse_shared_text(imported["text"], body.default_stay_min)
    result["warnings"].extend(imported["warnings"])
    comment_summary = summarize_comments(imported["comments"])
    llm_candidates = []
    llm_used = False
    llm_error = None
    if body.use_llm:
        llm_input = imported["text"]
        if imported["comments"]:
            llm_input += "\n\n【用户粘贴的评论，仅用于提取评论中明确提到的地点】\n" + imported["comments"]
        llm_candidates, llm_error = await llm_place_parser.parse(llm_input)
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

    # LLM 成功时采用其文本候选，避免规则把整句自然语言误合并成额外地点；
    # 高德链接候选在上方独立保留。LLM 失败或未启用时仍完整回退规则解析。
    text_candidates = [] if llm_used else list(result["candidates"])
    for candidate in llm_candidates if llm_used else []:
        text_candidates.append({
            **candidate,
            "expected_stay_min": body.default_stay_min,
            "source_url": result["source_urls"][0] if result["source_urls"] else None,
        })

    for candidate in text_candidates:
        city = str(candidate.get("city") or body.preferred_city or _infer_city(body.text)).strip()
        query = candidate["name"]
        poi_candidates = await amap.search_pois(query, city, 5) if city else []
        exact = [
            place for place in poi_candidates
            if _normalized_place_name(place["name"]) == _normalized_place_name(candidate["name"])
        ]
        # 唯一精确名称可自动定位；其他情况必须让用户从 POI 候选中确认。
        selected = exact[0] if len(exact) == 1 else None
        if not poi_candidates and candidate.get("address"):
            location = await amap.geocode(f"{city}{candidate['address']}" if city else candidate["address"])
            selected = ({
                "name": candidate["name"], "address": candidate["address"], "city": city,
                "district": "", "category": candidate.get("category", ""), **location,
            } if location else None)
        item = {
            **candidate,
            "city": city,
            "lat": selected["lat"] if selected else None,
            "lng": selected["lng"] if selected else None,
            "formatted_address": selected.get("address", "") if selected else None,
            "resolved": bool(selected),
            "resolution_error": None if selected else (
                "找到多个同名或相似地点，请选择具体门店" if poi_candidates
                else (amap.last_error or "无法确定地点坐标，请补充城市或详细地址")
            ),
            "resolution_source": "poi_exact" if selected and poi_candidates else ("geocode" if selected else None),
            "location_candidates": poi_candidates,
        }
        duplicate = selected and any(
            abs(float(existing["lat"]) - float(selected["lat"])) < 0.00001
            and abs(float(existing["lng"]) - float(selected["lng"])) < 0.00001
            for existing in resolved if existing.get("lat") is not None and existing.get("lng") is not None
        )
        if not duplicate:
            resolved.append(item)

    if resolved:
        warnings = [warning for warning in warnings if not warning.startswith("没有识别到地点")]
    parser = "rules_v1+amap_share_v1" + ("+llm_v1" if llm_used else "")
    parse_session_id = str(uuid4())
    linked_candidates = associate_comments_to_candidates(comment_summary, resolved[:10])
    candidates = [{**candidate, "parse_candidate_id": str(uuid4())} for candidate in linked_candidates]
    return {
        **result, "candidates": candidates, "warnings": warnings, "parser": parser,
        "parse_session_id": parse_session_id,
        "source_platform": body.source_platform, "comment_summary": comment_summary,
        "source_adapter": imported["adapter"],
        "llm_requested": body.use_llm, "llm_used": llm_used,
        "llm_usage": llm_place_parser.last_usage if body.use_llm else None,
    }
