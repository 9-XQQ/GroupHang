"""Phase 2B 多起点集合 + 多地点共同路线规划。"""
import asyncio
from math import ceil
from datetime import datetime, timedelta

from .amap import amap, _haversine_km
from .agent_trace import traced_stage

_SPEED = {"driving": 40.0, "transit": 15.0}


def _coord(item: dict) -> tuple[float, float]:
    return float(item["lat"]), float(item["lng"])


@traced_stage("route.segment", outcome="route")
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


def _schedule_order(
    order: list[int], destinations: list[dict], group_start: datetime,
    duration_matrix: list[list[float]],
) -> dict:
    """计算包含等待的时间线；固定窗要求完整停留落入窗口。"""
    cursor = group_start
    items = []
    for position, idx in enumerate(order):
        if position:
            cursor += timedelta(minutes=duration_matrix[order[position - 1]][idx])
        raw_arrival = cursor
        window_start = destinations[idx].get("visit_window_start")
        window_end = destinations[idx].get("visit_window_end")
        if window_start and cursor < window_start:
            cursor = window_start
        arrival = cursor
        leave = arrival + timedelta(minutes=destinations[idx]["expected_stay_min"])
        late_min = max(0.0, (leave - window_end).total_seconds() / 60) if window_end else 0.0
        items.append({
            "idx": idx, "raw_arrival": raw_arrival, "arrival": arrival, "leave": leave,
            "wait_min": max(0.0, (arrival - raw_arrival).total_seconds() / 60),
            "late_min": late_min,
        })
        cursor = leave
    return {
        "items": items, "end": cursor,
        "window_penalty": sum(item["late_min"] for item in items),
    }


def _time_aware_order(
    first: int, destinations: list[dict], group_start: datetime,
    duration_matrix: list[list[float]], route_matrix: list[list[float]],
) -> list[int]:
    remaining = set(range(len(destinations))) - {first}
    order = [first]
    while remaining:
        def candidate_key(idx: int) -> tuple:
            schedule = _schedule_order(order + [idx], destinations, group_start, duration_matrix)
            last = schedule["items"][-1]
            return (
                last["late_min"] > 0, last["late_min"],
                schedule["end"], route_matrix[order[-1]][idx],
            )
        nxt = min(remaining, key=candidate_key)
        order.append(nxt)
        remaining.remove(nxt)
    return order


def _improve_time_order(
    order: list[int], destinations: list[dict], group_start: datetime,
    duration_matrix: list[list[float]], route_matrix: list[list[float]],
    deadline: datetime, return_routes: list[dict], objective: str,
) -> list[int]:
    """有界插入搜索：先降低时间冲突，再改善目标成本；第一站保持不变。"""
    def key(candidate: list[int]) -> tuple:
        schedule = _schedule_order(candidate, destinations, group_start, duration_matrix)
        finish = schedule["end"]
        cost = _route_cost(candidate, route_matrix)
        if return_routes:
            back = return_routes[candidate[-1]]
            finish += timedelta(minutes=back["duration_min"])
            cost += back["distance_km" if objective == "distance" else "duration_min"]
        late = max(0.0, (finish - deadline).total_seconds() / 60)
        return (schedule["window_penalty"] + late, cost, finish, tuple(candidate))

    best = list(order)
    best_key = key(best)
    # 最多10站；每轮只接受完整时间线更优的单点插入，限制最多n轮。
    for _ in range(len(order)):
        improved, improved_key = best, best_key
        for old_pos in range(1, len(best)):
            shortened = best[:old_pos] + best[old_pos + 1:]
            for new_pos in range(1, len(best)):
                candidate = shortened[:new_pos] + [best[old_pos]] + shortened[new_pos:]
                if candidate == best:
                    continue
                candidate_key = key(candidate)
                if candidate_key < improved_key:
                    improved, improved_key = candidate, candidate_key
        if improved_key >= best_key:
            break
        best, best_key = improved, improved_key
    return best


async def plan_itinerary(
    participants: list[dict],
    destinations: list[dict],
    start_at: datetime,
    end_at: datetime,
    group_mode: str,
    objective: str,
    shared_route_cache: dict | None = None,
    return_location: dict | None = None,
) -> dict:
    """返回第一站、个人赴约、共同顺序和时间线。"""
    route_cache: dict[tuple, asyncio.Task] = shared_route_cache if shared_route_cache is not None else {}

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

    return_routes = []
    if return_location:
        async def return_route(destination):
            origin, target = _coord(destination), _coord(return_location)
            if origin == target:
                return {"duration_min": 0, "distance_km": 0, "polyline": [], "steps": [],
                        "estimated": False, "fallback_reason": None}
            return await cached_route(origin, target, group_mode)
        return_routes = await asyncio.gather(*(return_route(d) for d in destinations))
    deadline = min([end_at, *(p["available_until"] for p in participants if p.get("available_until"))])

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
        arrival_offsets = [(value["arrival_at"] - start_at).total_seconds() / 60 for value in arrivals]
        max_arrival = max(arrival_offsets)
        candidate_group_start = max(value["arrival_at"] for value in arrivals)
        has_windows = any(item.get("visit_window_start") for item in destinations)
        order = (
            _time_aware_order(first, destinations, candidate_group_start, duration_matrix, route_matrix)
            if has_windows else _two_opt(_greedy_order(first, route_matrix), route_matrix)
        )
        if has_windows:
            order = _improve_time_order(
                order, destinations, candidate_group_start, duration_matrix, route_matrix,
                deadline, return_routes, objective,
            )
        schedule = _schedule_order(order, destinations, candidate_group_start, duration_matrix)
        route_minutes = _route_cost(order, duration_matrix)
        route_km = _route_cost(order, distance_matrix)
        if return_location:
            route_minutes += return_routes[order[-1]]["duration_min"]
            route_km += return_routes[order[-1]]["distance_km"]
        availability_penalty = sum(
            1_000_000 for person, value in zip(participants, arrivals)
            if person.get("available_until") and value["arrival_at"] > person["available_until"]
        )
        window_penalty = schedule["window_penalty"] * 1_000_000
        finish = schedule["end"]
        if return_location:
            finish += timedelta(minutes=return_routes[order[-1]]["duration_min"])
        window_penalty += max(0, (finish - deadline).total_seconds() / 60) * 1_000_000
        if objective == "distance":
            score = route_km + max_arrival / 60.0 + availability_penalty + window_penalty
        elif objective == "total_time":
            score = route_minutes + max_arrival + availability_penalty + window_penalty
        else:
            spread = max_arrival - min(arrival_offsets)
            score = route_minutes + max_arrival + 0.25 * spread + availability_penalty + window_penalty
        if best is None or score < best["score"]:
            best = {"first": first, "order": order, "arrivals": arrivals, "score": score}

    order = best["order"]
    group_start = max(value["arrival_at"] for value in best["arrivals"])
    warnings = []

    def projected_end(current_order: list[int]) -> datetime:
        finish = _schedule_order(current_order, destinations, group_start, duration_matrix)["end"]
        if return_location:
            finish += timedelta(minutes=return_routes[current_order[-1]]["duration_min"])
        return finish

    # 固定时间窗冲突时，只允许剔除可选地点；优先选择能最大幅度降低迟到的地点。
    while _schedule_order(order, destinations, group_start, duration_matrix)["window_penalty"] > 0:
        baseline = _schedule_order(order, destinations, group_start, duration_matrix)["window_penalty"]
        choices = []
        for position in range(1, len(order)):
            idx = order[position]
            if destinations[idx]["visit_status"] != "optional":
                continue
            candidate = order[:position] + order[position + 1:]
            penalty = _schedule_order(candidate, destinations, group_start, duration_matrix)["window_penalty"]
            choices.append((baseline - penalty, position, idx))
        if not choices or max(choices)[0] <= 0:
            break
        _, position, removed_idx = max(choices)
        order.pop(position)
        warnings.append({
            "code": "optional_time_window_skipped", "destination_id": destinations[removed_idx]["id"],
            "message": f"无法满足固定到访时间，已跳过可选地点：{destinations[removed_idx]['name']}",
        })

    # 超时时优先剔除节省时间最多的可选点；必去点和第一站绝不移除。
    while projected_end(order) > deadline and len(order) > 2:
        removable = []
        for position in range(1, len(order)):
            idx = order[position]
            if destinations[idx]["visit_status"] != "optional":
                continue
            shortened = order[:position] + order[position + 1:]
            if (_schedule_order(shortened, destinations, group_start, duration_matrix)["window_penalty"]
                    > _schedule_order(order, destinations, group_start, duration_matrix)["window_penalty"]):
                continue  # 不能为缩短结束时间引入更严重的固定窗冲突
            saving = (projected_end(order) - projected_end(shortened)).total_seconds() / 60
            if saving <= 0:
                continue
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
                "late_min": ceil((route["arrival_at"] - person["available_until"]).total_seconds() / 60),
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
        window_start = destination.get("visit_window_start")
        window_end = destination.get("visit_window_end")
        wait_min = 0
        if window_start and arrival < window_start:
            wait_min = round((window_start - arrival).total_seconds() / 60)
            cursor = window_start
            arrival = cursor
            warnings.append({
                "code": "wait_for_visit_window", "destination_id": destination["id"],
                "wait_min": wait_min,
                "message": f"提前到达 {destination['name']}，需等待 {wait_min} 分钟后开始停留",
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
        for person in participants:
            available_until = person.get("available_until")
            if available_until and cursor > available_until and person["user_id"] not in availability_warned:
                availability_warned.add(person["user_id"])
                warnings.append({
                    "code": "participant_leaves_early", "user_id": person["user_id"],
                    "destination_id": destination["id"],
                    "late_min": ceil((cursor - available_until).total_seconds() / 60),
                    "message": f"{person['name']} 无法在个人结束时间前完成 {destination['name']} 的停留及后续行程",
                })
        if window_end and cursor > window_end:
            warnings.append({
                "code": "destination_time_window_conflict", "destination_id": destination["id"],
                "late_min": ceil((cursor - window_end).total_seconds() / 60),
                "message": f"{destination['name']} 无法在固定到访时间内完成停留",
            })
        ordered_stops.append({
            "destination_id": destination["id"], "name": destination["name"],
            "lat": destination["lat"], "lng": destination["lng"], "order_index": position + 1,
            "arrival_at": arrival.isoformat(), "leave_at": cursor.isoformat(),
            "expected_stay_min": destination["expected_stay_min"], "visit_status": destination["visit_status"],
            "visit_window_start": window_start.isoformat() if window_start else None,
            "visit_window_end": window_end.isoformat() if window_end else None,
            "wait_min": wait_min,
        })
    return_leg = None
    if return_location:
        last = order[-1]
        detail = return_routes[last]
        depart = cursor
        cursor += timedelta(minutes=detail["duration_min"])
        total_km += detail["distance_km"]
        return_leg = {
            "kind": "return", "from_destination_id": destinations[last]["id"], "to_destination_id": None,
            "return_location": dict(return_location), "mode": group_mode,
            "depart_at": depart.isoformat(), "arrival_at": cursor.isoformat(),
            "travel_time_min": round(detail["duration_min"]), "distance_km": round(detail["distance_km"], 2),
            "polyline": detail.get("polyline", []), "steps": detail.get("steps", []),
            "estimated": detail["estimated"], "fallback_reason": detail.get("fallback_reason"),
        }
        route_legs.append(return_leg)
        for person in participants:
            if person.get("available_until") and cursor > person["available_until"]:
                warnings.append({"code": "return_after_participant_deadline", "user_id": person["user_id"],
                                 "late_min": ceil((cursor - person["available_until"]).total_seconds() / 60),
                                 "message": f"返回共同终点将超过{person['name']}的个人结束时间"})
    if cursor > end_at:
        warnings.append({
            "code": "exceeds_end_time",
            "late_min": ceil((cursor - end_at).total_seconds() / 60),
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
        "route_legs": route_legs, "return_leg": return_leg, "finish_at": cursor.isoformat(), "warnings": warnings,
        "total_travel_min": round((group_start - start_at).total_seconds() / 60 + _route_cost(order, duration_matrix)
                                  + (return_routes[order[-1]]["duration_min"] if return_location else 0)),
        "total_duration_min": round((cursor - start_at).total_seconds() / 60),
        "total_distance_km": round(total_km, 2),
    }
