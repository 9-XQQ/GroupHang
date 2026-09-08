"""约点推荐核心算法。

流程：粗筛候选点（网格采样 + 周边热门 POI）→ 分出行方式算耗时 →
自驾叠加停车成本 → 按 minimax / min_sum 排名取 Top3。

关键约束（方案文档强调）：不同出行方式的人必须分开算耗时，不能混用一套时间。
"""
import math
from typing import Any

from .amap import amap, _haversine_km

# 自驾「最后一公里」固定成本（分钟）：找车位 + 停车步行到店
_DRIVING_PARK_MIN = 6.0
_DRIVING_WALK_MIN = 3.0

# 直线距离估算的参考速度（km/h），仅在无法调用高德时降级使用
_DRIVING_FALLBACK_SPEED = 40.0
_TRANSIT_FALLBACK_SPEED = 15.0


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    return _haversine_km(lat1, lng1, lat2, lng2)


async def generate_candidates(participants: list[dict], max_candidates: int = 15) -> list[dict]:
    """生成候选碰面点：包围盒内网格采样 + 中心点周边热门 POI。"""
    lats = [p["lat"] for p in participants]
    lngs = [p["lng"] for p in participants]
    min_lat, max_lat = min(lats), max(lats)
    min_lng, max_lng = min(lngs), max(lngs)
    center = ((min_lat + max_lat) / 2, (min_lng + max_lng) / 2)

    # 网格采样：步长自适应，保证每轴约 5~6 个点，避免候选爆炸
    span_lat = max(max_lat - min_lat, 0.005)
    span_lng = max(max_lng - min_lng, 0.005)
    step_lat = span_lat / 5.0
    step_lng = span_lng / 5.0

    grid: list[dict] = []
    lat = min_lat
    while lat <= max_lat + 1e-9:
        lng = min_lng
        while lng <= max_lng + 1e-9:
            grid.append(
                {
                    "name": f"网格点({lat:.3f},{lng:.3f})",
                    "lat": round(lat, 6),
                    "lng": round(lng, 6),
                    "category": "网格采样点",
                    "address": "",
                    "source": "grid",
                }
            )
            lng += step_lng
        lat += step_lat

    # 过滤：剔除离任一起点直线距离过远的点（> 30km）
    def _ok(c: dict) -> bool:
        return all(
            haversine_km(c["lat"], c["lng"], p["lat"], p["lng"]) <= 30 for p in participants
        )

    grid = [c for c in grid if _ok(c)]

    # 周边热门 POI（地铁站/商圈/餐饮），仅在高德可用时返回
    pois = (
        await amap.nearby_pois(center, radius=3000, types="150000|060000|050000")
        if amap.available
        else []
    )

    candidates = pois + grid
    # 按「到各起点的直线距离均衡度」排序（方差小者优先），再截断
    candidates.sort(key=lambda c: _balance_score(c, participants))
    return candidates[:max_candidates]


def _balance_score(candidate: dict, participants: list[dict]) -> float:
    dists = [haversine_km(candidate["lat"], candidate["lng"], p["lat"], p["lng"]) for p in participants]
    mean = sum(dists) / len(dists)
    var = sum((d - mean) ** 2 for d in dists) / len(dists)
    return var


def candidate_id(candidate: dict) -> str:
    """为 POI/网格点生成可跨刷新复用的稳定投票标识。"""
    if candidate.get("poi_id"):
        return f"poi:{candidate['poi_id']}"
    return f"coord:{float(candidate['lat']):.6f},{float(candidate['lng']):.6f}"


def _driving_time(p: dict, c: dict, dur_min: float | None) -> tuple[float, str]:
    """自驾：驾车 ETA + 停车成本。"""
    if dur_min is None:
        base = haversine_km(p["lat"], p["lng"], c["lat"], c["lng"]) / _DRIVING_FALLBACK_SPEED * 60
        summary = f"驾车约 {base:.0f} 分钟（直线距离估算，路况数据暂不可用）"
    else:
        base = dur_min
        summary = f"驾车约 {base:.0f} 分钟（含实时路况估算）"
    total = base + _DRIVING_PARK_MIN + _DRIVING_WALK_MIN
    return total, summary


async def _transit_time(p: dict, c: dict) -> tuple[float, str]:
    """公交/步行/打车：优先公交路径规划，失败降级直线估算。"""
    dur = await amap.transit_duration((p["lat"], p["lng"]), (c["lat"], c["lng"]))
    if dur is None:
        base = haversine_km(p["lat"], p["lng"], c["lat"], c["lng"]) / _TRANSIT_FALLBACK_SPEED * 60
        summary = f"公交约 {base:.0f} 分钟（直线距离估算）"
    else:
        base = dur
        summary = f"公交约 {base:.0f} 分钟"
    return base, summary


async def recommend(participants: list[dict], objective: str | None = None) -> list[dict]:
    """计算并返回 Top3 候选点。

    participants: [{user_id, name, mode('driving'|'transit'), lat, lng}]
    objective: 'minimax'(公平,默认) | 'min_sum'(效率)
    """
    candidates = await generate_candidates(participants)
    if not candidates:
        return []

    scored: list[dict] = []
    for c in candidates:
        driving_ps = [p for p in participants if p["mode"] == "driving"]
        transit_ps = [p for p in participants if p["mode"] != "driving"]

        # 驾车组：批量算（1 个终点对多个起点）
        driving_by_user_id: dict[int, float | None] = {}
        if driving_ps:
            origins = [f"{p['lng']},{p['lat']}" for p in driving_ps]
            driving_map = await amap.driving_durations_batch(origins, (c["lat"], c["lng"]))
            driving_by_user_id = {
                p["user_id"]: driving_map.get(driving_idx)
                for driving_idx, p in enumerate(driving_ps)
            }

        # 停车信息：只依赖目的地，每候选点取一次，共享给所有自驾者
        parking: dict[str, Any] = {"difficulty": "周末商圈车位可能紧张", "lots": []}
        if driving_ps and amap.available:
            lots = await amap.parking_nearby((c["lat"], c["lng"]))
            if lots:
                parking = {"difficulty": "附近有停车场，建议预留找车位时间", "lots": lots}

        per_person: list[dict] = []
        times: list[float] = []
        for p in participants:
            if p["mode"] == "driving":
                dur = driving_by_user_id.get(p["user_id"])
                mins, summary = _driving_time(p, c, dur)
                per_person.append(
                    {
                        "user_id": p["user_id"],
                        "name": p["name"],
                        "mode": p["mode"],
                        "travel_time_min": round(mins),
                        "route_summary": summary,
                        "parking": parking,
                    }
                )
            else:
                mins, summary = await _transit_time(p, c)
                per_person.append(
                    {
                        "user_id": p["user_id"],
                        "name": p["name"],
                        "mode": p["mode"],
                        "travel_time_min": round(mins),
                        "route_summary": summary,
                        "parking": None,
                    }
                )
            times.append(mins)

        scored.append(
            {
                "candidate_id": candidate_id(c),
                "poi": {
                    "name": c["name"],
                    "address": c.get("address", ""),
                    "lat": c["lat"],
                    "lng": c["lng"],
                    "category": c["category"],
                },
                "metrics": {
                    "max_travel_time_min": round(max(times)),
                    "total_travel_time_min": round(sum(times)),
                },
                "per_person": per_person,
            }
        )

    # 排名：minimax 公平优先 / min_sum 效率优先
    key = (lambda s: s["metrics"]["max_travel_time_min"]) if objective != "min_sum" else (
        lambda s: s["metrics"]["total_travel_time_min"]
    )
    scored.sort(key=key)
    top = scored[:3]
    for rank, s in enumerate(top, start=1):
        s["rank"] = rank
        s["objective"] = objective or "minimax"
    return top
