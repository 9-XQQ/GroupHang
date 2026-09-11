"""地图地址和 POI 搜索。"""
from fastapi import APIRouter, HTTPException, Query

from ..services.amap import amap

router = APIRouter(prefix="/api/places", tags=["places"])


@router.get("/search")
async def search_places(
    keywords: str = Query(min_length=1, max_length=120),
    city: str = Query(min_length=1, max_length=30),
    limit: int = Query(default=10, ge=1, le=20),
) -> dict:
    """在明确的城市范围内搜索地点，由前端展示候选项供用户确认。"""
    normalized_keywords = keywords.strip()
    normalized_city = city.strip()
    if not normalized_keywords or not normalized_city:
        raise HTTPException(status_code=422, detail="城市和搜索关键词不能为空")
    results = await amap.search_pois(normalized_keywords, normalized_city, limit)
    if not results and amap.last_error:
        raise HTTPException(status_code=503, detail=f"地点搜索暂不可用：{amap.last_error}")
    return {
        "keywords": normalized_keywords,
        "city": normalized_city,
        "results": results,
    }
