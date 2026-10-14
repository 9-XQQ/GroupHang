"""Tool-backed meal, branch and local afternoon discovery. No LLM route facts."""
import asyncio
import re
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError
from sqlalchemy import select

from ..models import SavedPlace, TripParticipant, User
from ..schemas import DiscoveryRequest, SavedPlaceCreate
from .amap import amap, _haversine_km
from .itinerary import _route
from .llm_parser import llm_place_parser

MAX_CANDIDATES = 8
MAX_PARTICIPANTS = 10


def scene_from_message(message):
    if re.search(r"半日|(?:附近|周边).*(?:玩|游)|(?:玩|游玩|逛).{0,6}一?下午", message):
        return "afternoon"
    if re.search(r"分店|哪一家|哪家店|连锁", message):
        return "brand"
    if re.search(r"去哪吃|吃什么|不知道.*吃|推荐.*(?:餐|吃)|(?:一起|大家|我们|约).*(?:吃饭|聚餐)|(?:吃饭|聚餐).*(?:推荐|哪|不知)", message):
        return "meal"
    return None


async def interpret(body: DiscoveryRequest):
    data = body.model_dump()
    data["scenario"] = body.scenario or scene_from_message(body.message)
    # Only parse explicit values. Missing dates/times are returned as questions.
    date = re.search(r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?", body.message)
    times = re.findall(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)", body.message)
    if date and len(times) >= 2 and not body.start_at and not body.end_at:
        try:
            y, m, d = map(int, date.groups())
            for field, (h, minute) in zip(("start_at", "end_at"), times[:2]):
                data[field] = datetime(y, m, d, int(h), int(minute), tzinfo=timezone(timedelta(hours=8)))
        except ValueError:
            pass
    anchor = re.search(r"(?:我想要?|想|在)?在?([^，。！？]{2,30}?)(?:附近|周边)", body.message)
    if anchor and not body.anchor_name and not body.anchor:
        name = re.sub(r"^(?:我想要?|想|在)+", "", anchor.group(1)).strip()
        data["anchor_name"] = name
    if not body.brand and data["scenario"] == "brand":
        brand = re.search(r"(?:吃|选择|选|去)([^，。！？\s]{2,20}?)(?=的?分店|，|。|但是|但|哪一家|哪家|$)", body.message)
        if brand:
            data["brand"] = re.sub(r"^(?:吃|去)", "", brand.group(1)).strip()
    # LLM only extracts intent/keywords, never sees origins, saved notes or preferences.
    if body.message and (not data["scenario"] or (data["scenario"] == "brand" and not data["brand"])):
        payload, _ = await llm_place_parser.complete_json(
            '只提取出行意图，输出JSON：{"scenario":"meal|brand|afternoon","brand":"","anchor_name":""}。'
            'meal是不知道去哪吃，brand是指定品牌选分店，afternoon是某地附近游玩。不得猜品牌或地点。',
            body.message)
        if isinstance(payload, dict):
            if not data["scenario"] and payload.get("scenario") in {"meal", "brand", "afternoon"}:
                data["scenario"] = payload["scenario"]
            for field in ("brand", "anchor_name"):
                value = payload.get(field)
                if not data[field] and isinstance(value, str) and len(value) <= (60 if field == "brand" else 120):
                    # Keywords must appear in the user's message, not be invented.
                    if value and value in body.message:
                        data[field] = value
    return DiscoveryRequest.model_validate(data)


def public_candidate(saved):
    """Private note is deliberately excluded even from deterministic planner inputs."""
    return {"name": saved.name, "address": saved.address or "", "city": saved.city,
            "lat": saved.lat, "lng": saved.lng, "category": saved.category or "",
            "poi_id": saved.poi_id, "tags": saved.tags or [], "expected_stay_min": saved.expected_stay_min,
            "saved_place_id": saved.id, "revision": saved.revision, "source": "saved"}


def selection(place):
    if place.get("saved_place_id"):
        return {"saved_place_id": place["saved_place_id"], "revision": place["revision"]}
    values = SavedPlaceCreate.model_validate({
        key: place[key] for key in ("name", "city", "address", "lat", "lng", "category", "poi_id", "expected_stay_min")
        if key in place})
    return {"place": values.model_dump(mode="json")}


def distance(a, b):
    return _haversine_km(a["lat"], a["lng"], b["lat"], b["lng"])


async def discover(db, trip, user, body):
    try:
        request = await interpret(body)
    except ValidationError:
        return {"needs": ["无法识别时间或条件，请使用带日期的开始/结束时间"], "scenario": body.scenario}
    response = {"scenario": request.scenario, "input_version": trip.input_version, "saved": False,
                "recommendations": [], "warnings": [], "needs": []}
    if not request.scenario:
        response["needs"] = ["请选择多人吃饭、品牌分店或附近半日游场景"]
        return response
    rows = (await db.execute(select(TripParticipant, User).join(
        User, User.id == TripParticipant.user_id).where(TripParticipant.trip_id == trip.id))).all()
    if not rows or len(rows) > MAX_PARTICIPANTS:
        response["needs"] = ["本次推荐支持1～10名参与者"]
        return response
    people = []
    for member, account in rows:
        if not member.start_location:
            response["needs"].append(account.name + "尚未填写出发点")
            continue
        shared = request.include_preferences and (account.id == user.id or member.share_preferences)
        prefs = account.preferences or {}
        explicit = prefs.get("explicit", {}) if shared and prefs.get("personalization_enabled", True) else {}
        people.append({"user_id": account.id, "name": account.name, "mode": member.transport_mode,
                       **member.start_location, "available_from": member.available_from,
                       "available_until": member.available_until,
                       "preferred_categories": explicit.get("preferred_categories", []),
                       "avoid_tags": explicit.get("avoid_tags", [])})
    if response["needs"]:
        return response
    await db.commit()
    center = {"lat": sum(p["lat"] for p in people) / len(people),
              "lng": sum(p["lng"] for p in people) / len(people)}
    start, end = request.start_at or trip.planned_start_at, request.end_at or trip.planned_end_at
    if (start is None) != (end is None) or (start and not 0 < (end - start).total_seconds() <= 86400):
        response["needs"] = ["请提供24小时内完整且有效的开始和结束时间"]
        return response
    if request.scenario == "afternoon" and not (start and end):
        response["needs"] = ["请确认游玩日期、开始和结束时间，例如2026-10-18 13:00～18:00"]
        return response
    if request.scenario == "brand" and not request.brand:
        response["needs"] = ["请填写要比较的品牌，例如寿司郎"]
        return response
    anchor = request.anchor.model_dump() if request.anchor else None
    if request.anchor_name and not anchor:
        if not request.city:
            response["needs"] = ["请填写锚点所在城市，避免同名地点误选"]
            return response
        matches = await amap.search_pois(request.anchor_name, request.city, 5)
        exact = [p for p in matches if p["name"] == request.anchor_name]
        if len(exact) == 1:
            anchor = exact[0]
        elif len(matches) == 1:
            anchor = matches[0]
        else:
            response["needs"] = ["锚点无法唯一定位，请从搜索结果或地图明确选择"]
            response["anchor_choices"] = matches
            return response
    if request.scenario == "afternoon" and not anchor:
        response["needs"] = ["请填写游玩区域，或在地图选点后使用该位置"]
        return response
    center = anchor or center
    filters = [SavedPlace.user_id == user.id]
    if request.city:
        filters.append(SavedPlace.city == request.city)
    if request.tags:
        filters.append(SavedPlace.tags.contains(request.tags))
    saved = (await db.execute(select(SavedPlace).where(*filters).order_by(
        SavedPlace.id.desc()).limit(101))).scalars().all()
    await db.commit()
    if len(saved) > 100:
        response["warnings"].append("收藏筛选只读取最近100个，请用城市和标签缩小范围")
    candidates = [public_candidate(p) for p in saved[:100]]
    if request.scenario == "brand":
        candidates = [p for p in candidates if request.brand.casefold() in p["name"].casefold()]
    elif request.scenario == "meal":
        candidates = [p for p in candidates if re.search(
            r"餐|食|饭|寿司|火锅|烧烤|小吃|面|咖啡|茶|料理|food|restaurant", p["category"] + p["name"] + " ".join(p["tags"]), re.I)]
    if not request.saved_only:
        if request.scenario == "brand":
            if not request.city:
                response["needs"] = ["比较品牌分店需要明确城市"]
                return response
            online = await amap.search_pois(request.brand, request.city, 20)
            online = [p for p in online if request.brand.casefold() in p["name"].casefold()]
        else:
            online = await amap.nearby_pois((center["lat"], center["lng"]),
                radius=int(request.radius_km * 1000), types="050000" if request.scenario == "meal" else "110000|140000|060000")
        candidates += [{**p, "source": "poi", "city": p.get("city") or request.city,
                        "expected_stay_min": request.stay_min} for p in online]
        if not online:
            response["warnings"].append("在线地点搜索无结果或不可用；只使用符合条件的收藏，不虚构地点")
    unique = {}
    excluded = []
    for place in candidates:
        try:
            SavedPlaceCreate.model_validate({key: place[key] for key in
                ("name", "city", "address", "lat", "lng", "category", "poi_id", "expected_stay_min") if key in place})
        except ValidationError:
            continue
        if distance(center, place) > request.radius_km:
            excluded.append({"name": place["name"], "reason": "超过本次中心半径"})
            continue
        key = place.get("poi_id") or (place["name"], round(place["lat"], 5), round(place["lng"], 5))
        if key not in unique:
            unique[key] = place
    candidates = sorted(unique.values(), key=lambda p: max(distance(person, p) for person in people))[:MAX_CANDIDATES]
    response.update({"candidate_count": len(candidates), "candidate_limit": MAX_CANDIDATES,
                     "center": {"lat": center["lat"], "lng": center["lng"]},
                     "radius_km": request.radius_km, "excluded": excluded[:20],
                     "start_at": start, "end_at": end, "group_transport_mode": trip.group_transport_mode,
                     "optimization_objective": trip.optimization_objective})
    response["warnings"].append("候选有界筛选，结果不保证全局最优；营业、排队和清净程度未核验")
    if request.tags and not request.saved_only:
        response["warnings"].append("自定义标签用于筛选收藏，在线POI没有私人标签")
    if not candidates:
        return response
    cache = {}
    async def route(origin, target, mode):
        key = (origin["lat"], origin["lng"], target["lat"], target["lng"], mode)
        if key not in cache:
            cache[key] = await _route((origin["lat"], origin["lng"]), (target["lat"], target["lng"]), mode)
        return cache[key]
    async def arrivals(place):
        routes = await asyncio.gather(*(route(p, place, p["mode"]) for p in people))
        items = []
        for p, leg in zip(people, routes):
            departure = max(start, p["available_from"]) if start and p["available_from"] else start
            at = departure + timedelta(minutes=leg["duration_min"]) if departure else None
            items.append({"user_id": p["user_id"], "name": p["name"], "mode": p["mode"],
                          "travel_min": round(leg["duration_min"], 1), "arrival_at": at,
                          "estimated": leg["estimated"], "fallback_reason": leg.get("fallback_reason"),
                          "distance_km": leg["distance_km"]})
        return items
    if request.scenario in {"meal", "brand"}:
        scored = []
        for place in candidates:
            per_person = await arrivals(place)
            meeting = max(item["arrival_at"] for item in per_person) if start else None
            finish = meeting + timedelta(minutes=place["expected_stay_min"]) if meeting else None
            deadlines = [p["available_until"] for p in people if p["available_until"]]
            deadline = min([end, *deadlines]) if end else None
            if finish and deadline and finish > deadline:
                response["excluded"].append({"name": place["name"], "reason": "交通及用餐停留超过可用时段"})
                continue
            maximum = max(p["travel_min"] for p in per_person)
            total = sum(p["travel_min"] for p in per_person)
            scored.append({"place": place, "per_person": per_person, "max_travel_min": maximum,
                           "total_travel_min": round(total, 1), "meeting_at": meeting, "finish_at": finish})
        for item in scored:
            place = item["place"]
            item["nearby"] = (await amap.nearby_pois((place["lat"], place["lng"]), radius=1000, types="110000|060100"))[:5] if amap.available else []
            text = " ".join(q["name"] + q.get("category", "") for q in [place, *item["nearby"]])
            item["preference_matches"] = sum(1 for p in people for category in p["preferred_categories"] if category in text)
        # Preferences only break ties within the same five-minute fairness band.
        scored.sort(key=lambda p: (int(p["max_travel_min"] // 5), -p["preference_matches"], p["max_travel_min"], p["total_travel_min"]))
        for item in scored[:3]:
            place = item["place"]
            nearby = item["nearby"]
            reasons = [f"最远一人约{item['max_travel_min']}分钟，合计约{item['total_travel_min']}分钟", "公平耗时按5分钟分组，同组优先匹配本次启用的偏好"]
            text = place["name"] + (place.get("address") or "") + place.get("category", "")
            if "商场" in text or "购物中心" in text:
                reasons.append("地点名称/地址标注商场或购物中心")
            if nearby:
                reasons.append("1公里内POI：" + "、".join(p["name"] for p in nearby))
            for p in people:
                for category in p["preferred_categories"]:
                    hits = [q["name"] for q in [place, *nearby] if category in q.get("category", "") + q["name"]]
                    if hits:
                        reasons.append(p["name"] + "本次启用的偏好“" + category + "”匹配：" + "、".join(hits[:2]))
                if any("清净" in value or "拥挤" in value for value in p["preferred_categories"] + p["avoid_tags"]):
                    reasons.append(p["name"] + "关注环境清净，但本次没有可靠拥挤/安静数据")
            item.update({"reasons": reasons, "nearby": nearby, "selection": selection(place),
                         "estimated": any(p["estimated"] for p in item["per_person"])})
            response["recommendations"].append(item)
        response["warnings"].append("比较范围为中心半径；未含饭后返程。若未设置时段，只比较赴约交通")
    else:
        deadlines = [p["available_until"] for p in people if p["available_until"]]
        deadline = min([end, *deadlines])
        ordered, cursor, current = [], start, None
        remaining = list(candidates)
        while remaining and len(ordered) < 5:
            options = []
            for place in remaining:
                if current is None:
                    per_person = await arrivals(place)
                    arrival = max(item["arrival_at"] for item in per_person)
                    leg = None
                    estimated = any(p["estimated"] for p in per_person)
                else:
                    leg = await route(current, place, trip.group_transport_mode)
                    arrival = cursor + timedelta(minutes=leg["duration_min"])
                    per_person, estimated = [], leg["estimated"]
                leave = arrival + timedelta(minutes=place["expected_stay_min"])
                return_leg = await route(place, anchor, trip.group_transport_mode) if request.return_to_anchor else None
                finish = leave + timedelta(minutes=return_leg["duration_min"]) if return_leg else leave
                if finish <= deadline:
                    pref = sum(1 for p in people for category in p["preferred_categories"]
                               if category in place["name"] + place.get("category", ""))
                    options.append((arrival, -pref, place, leave, per_person, leg, estimated, return_leg))
            if not options:
                break
            arrival, _, place, leave, per_person, leg, estimated, return_leg = min(options, key=lambda x: (int((x[0] - cursor).total_seconds() // 300), x[1], x[0]))
            ordered.append({"place": place, "arrival_at": arrival, "leave_at": leave,
                "wait_min": max(0, (arrival - min(p["arrival_at"] for p in per_person)).total_seconds() / 60) if per_person else 0,
                "per_person": per_person, "leg": leg, "estimated": estimated,
                "selection": selection(place), "reasons": ["在指定范围内，交通与停留可纳入本次时段"]})
            cursor, current = leave, place
            remaining.remove(place)
        for place in remaining:
            response["excluded"].append({"name": place["name"], "reason": "有界贪心选择后未纳入剩余时间，或已达5站上限"})
        back = await route(current, anchor, trip.group_transport_mode) if current and request.return_to_anchor else None
        response.update({"recommendations": ordered, "return_leg": back,
                         "finish_at": cursor + timedelta(minutes=back["duration_min"]) if back else cursor,
                         "return_to_anchor": request.return_to_anchor,
                         "return_location": {"lat": anchor["lat"], "lng": anchor["lng"], "address": anchor.get("address") or anchor.get("name")} if request.return_to_anchor else None,
                         "feasibility": "no_plan" if not ordered else "estimated" if any(p["estimated"] for p in ordered) or (back and back["estimated"]) else "route_checked"})
        response["warnings"].append("未要求返回时，结束时间为末站离开时间；预览不包含其他未选中的原行程地点")
    return response
