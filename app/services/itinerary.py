"""Phase 2B 多起点集合 + 多地点共同路线规划。"""
import asyncio
from datetime import datetime, timedelta

from .amap import amap, _haversine_km

_SPEED = {"driving": 40.0, "transit": 15.0}


def _coord(item: dict) -> tuple[float, float]:
    return float(item["lat"]), float(item["lng"])


async def _route(origin: tuple, dest: tuple, mode: str) -> dict:
    detail = await amap.route_detail(origin, dest, mode)
    if detail and detail.get("duration_min") is not None and detail.get("polyline"):
        if not detail.get("distance_km"):
            detail["distance_km"] = round(_haversine_km(origin[0], origin[1], dest[0], dest[1]), 2)
        return {**detail, "estimated": False, "fallback_reason": None}
    km = _haversine_km(origin[0], origin[1], dest[0], dest[1])
    return {
        "duration_min": km / _SPEED[mode] * 60, "distance_km": round(km, 2),
        "polyline": [[origin[1], origin[0]], [dest[1], dest[0]]], "steps": [], "estimated": True,
        "fallback_reason": amap.last_error or "高德未返回可用路线",
    }


def _greedy_order(first: int, matrix: list[list[float]]) -> list[int]:
    remaining = set(range(len(matrix))) - {first}
    order = [first]
    while remaining:
        current = order[-1]
        nxt = min(remaining, key=lambda idx: matrix[current][idx])
        order.append(nxt)
        remaining.remove(nxt)
    return order


def _route_cost(order: list[int], matrix: list[list[float]]) -> float:
    return sum(matrix[a][b] for a, b in zip(order, order[1:]))


def _two_opt(order: list[int], matrix: list[list[float]]) -> list[int]:
    best = order
    improved = True
    while improved:
        improved = False
        for i in range(1, len(best) - 1):  # 固定第一站
            for j in range(i + 1, len(best)):
                candidate = best[:i] + list(reversed(best[i : j + 1])) + best[j + 1 :]
                if _route_cost(candidate, matrix) + 1e-9 < _route_cost(best, matrix):
                    best, improved = candidate, True
    return best


async def plan_itinerary(
    participants: list[dict],
    destinations: list[dict],
    start_at: datetime,
    end_at: datetime,
    group_mode: str,
    objective: str,
) -> dict:
    """返回第一站、个人赴约、共同顺序和时间线。"""
    route_cache: dict[tuple, asyncio.Task] = {}

    async def cached_route(origin: tuple, dest: tuple, mode: str) -> dict:
        key = (
            round(origin[0], 6), round(origin[1], 6),
            round(dest[0], 6), round(dest[1], 6), mode,
        )
        if key not in route_cache:
            route_cache[key] = asyncio.create_task(_route(origin, dest, mode))
        return await route_cache[key]

    size = len(destinations)
    distance_matrix = [[0.0] * size for _ in range(size)]
    duration_matrix = [[0.0] * size for _ in range(size)]
    estimated_matrix = [[False] * size for _ in range(size)]
    polyline_matrix = [[[] for _ in range(size)] for _ in range(size)]
    route_detail_matrix = [[{} for _ in range(size)] for _ in range(size)]
    async def load_matrix_item(i: int, j: int) -> None:
            a, b = _coord(destinations[i]), _coord(destinations[j])
            route = await cached_route(a, b, group_mode)
            distance_matrix[i][j] = route["distance_km"]
            duration_matrix[i][j] = route["duration_min"]
            estimated_matrix[i][j] = route["estimated"]
            polyline_matrix[i][j] = route["polyline"]
            route_detail_matrix[i][j] = route

    await asyncio.gather(*(
        load_matrix_item(i, j)
        for i in range(size) for j in range(size) if i != j
    ))

    best = None
    must_first = [i for i, item in enumerate(destinations) if item["visit_status"] == "must_visit"]
    first_candidates = must_first or list(range(size))

    async def participant_arrival(person: dict, first: int) -> dict:
        route = await cached_route(_coord(person), _coord(destinations[first]), person["mode"])
        depart_at = max(start_at, person.get("available_from") or start_at)
        arrival_at = depart_at + timedelta(minutes=route["duration_min"])
        return {**route, "depart_at": depart_at, "arrival_at": arrival_at}

    arrivals_by_first = {
        first: await asyncio.gather(*(participant_arrival(person, first) for person in participants))
        for first in first_candidates
    }

    for first in first_candidates:
        arrivals = arrivals_by_first[first]
        route_matrix = distance_matrix if objective == "distance" else duration_matrix
        order = _two_opt(_greedy_order(first, route_matrix), route_matrix)
        arrival_offsets = [(value["arrival_at"] - start_at).total_seconds() / 60 for value in arrivals]
        max_arrival = max(arrival_offsets)
        route_minutes = _route_cost(order, duration_matrix)
        route_km = _route_cost(order, distance_matrix)
        availability_penalty = sum(
            1_000_000 for person, value in zip(participants, arrivals)
            if person.get("available_until") and value["arrival_at"] > person["available_until"]
        )
        if objective == "distance":
            score = route_km + max_arrival / 60.0 + availability_penalty
        elif objective == "total_time":
            score = route_minutes + max_arrival + availability_penalty
        else:
            spread = max_arrival - min(arrival_offsets)
            score = route_minutes + max_arrival + 0.25 * spread + availability_penalty
        if best is None or score < best["score"]:
            best = {"first": first, "order": order, "arrivals": arrivals, "score": score}

    order = best["order"]
    group_start = max(value["arrival_at"] for value in best["arrivals"])
    warnings = []

    def projected_end(current_order: list[int]) -> datetime:
        travel = _route_cost(current_order, duration_matrix)
        stays = sum(destinations[idx]["expected_stay_min"] for idx in current_order)
        return group_start + timedelta(minutes=travel + stays)

    # 超时时优先剔除节省时间最多的可选点；必去点和第一站绝不移除。
    while projected_end(order) > end_at and len(order) > 2:
        removable = []
        for position in range(1, len(order)):
            idx = order[position]
            if destinations[idx]["visit_status"] != "optional":
                continue
            prev_idx = order[position - 1]
            saving = destinations[idx]["expected_stay_min"] + duration_matrix[prev_idx][idx]
            if position + 1 < len(order):
                next_idx = order[position + 1]
                saving += duration_matrix[idx][next_idx] - duration_matrix[prev_idx][next_idx]
            removable.append((saving, position, idx))
        if not removable:
            break
        _, position, removed_idx = max(removable)
        order.pop(position)
        warnings.append({
            "code": "optional_skipped", "destination_id": destinations[removed_idx]["id"],
            "message": f"时间不足，已自动跳过可选地点：{destinations[removed_idx]['name']}",
        })
    participant_arrivals = []
    for person, route in zip(participants, best["arrivals"]):
        minutes = route["duration_min"]
        participant_arrivals.append({
            "user_id": person["user_id"], "name": person["name"], "mode": person["mode"],
            "origin": {"lat": person["lat"], "lng": person["lng"]},
            "travel_time_min": round(minutes),
            "depart_at": route["depart_at"].isoformat(), "arrival_at": route["arrival_at"].isoformat(),
            "available_from": person.get("available_from").isoformat() if person.get("available_from") else None,
            "available_until": person.get("available_until").isoformat() if person.get("available_until") else None,
            "distance_km": route["distance_km"], "polyline": route["polyline"],
            "steps": route.get("steps", []), "estimated": route["estimated"],
            "fallback_reason": route.get("fallback_reason"),
        })
        if person.get("available_until") and route["arrival_at"] > person["available_until"]:
            warnings.append({
                "code": "participant_cannot_reach_first_stop", "user_id": person["user_id"],
                "message": f"{person['name']} 无法在个人结束时间前到达第一站",
            })

    cursor = group_start
    ordered_stops, route_legs = [], []
    total_km = 0.0
    availability_warned: set[int] = set()
    for position, idx in enumerate(order):
        destination = destinations[idx]
        if position:
            prev = order[position - 1]
            minutes = duration_matrix[prev][idx]
            km = distance_matrix[prev][idx]
            route_legs.append({
                "from_destination_id": destinations[prev]["id"], "to_destination_id": destination["id"],
                "mode": group_mode, "travel_time_min": round(minutes), "distance_km": round(km, 2),
                "polyline": polyline_matrix[prev][idx], "estimated": estimated_matrix[prev][idx],
                "steps": route_detail_matrix[prev][idx].get("steps", []),
                "fallback_reason": route_detail_matrix[prev][idx].get("fallback_reason"),
            })
            cursor += timedelta(minutes=minutes)
            total_km += km
        arrival = cursor
        for person in participants:
            available_until = person.get("available_until")
            if available_until and arrival > available_until and person["user_id"] not in availability_warned:
                availability_warned.add(person["user_id"])
                warnings.append({
                    "code": "participant_leaves_early", "user_id": person["user_id"],
                    "destination_id": destination["id"],
                    "message": f"{person['name']} 的个人结束时间早于到达 {destination['name']}，该参与者无法完成后续行程",
                })
        hours = destination.get("opening_hours")
        if hours:
            open_hour, open_minute = map(int, hours["open"].split(":"))
            close_hour, close_minute = map(int, hours["close"].split(":"))
            opening = arrival.replace(hour=open_hour, minute=open_minute, second=0, microsecond=0)
            closing = arrival.replace(hour=close_hour, minute=close_minute, second=0, microsecond=0)
            if arrival < opening or arrival > closing:
                warnings.append({
                    "code": "outside_opening_hours", "destination_id": destination["id"],
                    "message": f"{destination['name']} 预计到达时间不在营业时段 {hours['open']}-{hours['close']} 内",
                })
        cursor += timedelta(minutes=destination["expected_stay_min"])
        ordered_stops.append({
            "destination_id": destination["id"], "name": destination["name"],
            "lat": destination["lat"], "lng": destination["lng"], "order_index": position + 1,
            "arrival_at": arrival.isoformat(), "leave_at": cursor.isoformat(),
            "expected_stay_min": destination["expected_stay_min"], "visit_status": destination["visit_status"],
        })
    if cursor > end_at:
        warnings.append({
            "code": "exceeds_end_time",
            "message": f"预计超出结束时间 {round((cursor - end_at).total_seconds() / 60)} 分钟",
        })
    fallback_reasons = sorted({
        item.get("fallback_reason")
        for item in [*participant_arrivals, *route_legs]
        if item.get("estimated") and item.get("fallback_reason")
    })
    if any(p["estimated"] for p in participant_arrivals) or any(leg["estimated"] for leg in route_legs):
        reason_text = "；".join(fallback_reasons)
        warnings.append({
            "code": "estimated_routes",
            "message": f"部分路段为直线距离估算，不代表实时路况。原因：{reason_text or '高德未返回可用路线'}",
        })
    return {
        "first_destination_id": destinations[best["first"]]["id"],
        "participant_arrivals": participant_arrivals,
        "group_start_at": group_start.isoformat(), "ordered_stops": ordered_stops,
        "route_legs": route_legs, "warnings": warnings,
        "total_travel_min": round((group_start - start_at).total_seconds() / 60 + _route_cost(order, duration_matrix)),
        "total_duration_min": round((cursor - start_at).total_seconds() / 60),
        "total_distance_km": round(total_km, 2),
    }
