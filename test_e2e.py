"""Phase 1 端到端闭环测试脚本（零依赖，仅用标准库）。

跑法（确保服务已启动后执行）：
    python test_e2e.py

按顺序验证：
    1. 登录两个用户（小明/自驾、小红/公交）
    2. 小明创建 trip，拿到邀请码
    3. 小红用邀请码加入
    4. 两人各自填出发点 + 出行方式
    5. 触发约点推荐
    6. 打印 Top3 结果（分方式耗时对比）

可通过命令行参数覆盖默认地址：
    python test_e2e.py http://127.0.0.1:8000
"""
import json
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
REQUEST_TIMEOUT_SECONDS = 120


def request(method: str, path: str, body: dict | None = None, token: str | None = None) -> dict:
    """发 JSON 请求，返回解析后的 JSON（失败抛异常）。"""
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Token"] = token

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} 失败 ({e.code}): {detail}") from e


def expect_http_error(
    expected_status: int,
    method: str,
    path: str,
    body: dict | None = None,
    token: str | None = None,
) -> None:
    """断言请求被指定 HTTP 状态拒绝。"""
    try:
        request(method, path, body, token)
    except RuntimeError as exc:
        if f"({expected_status})" not in str(exc):
            raise
        return
    raise RuntimeError(f"{method} {path} 应返回 {expected_status}，实际请求成功")


def login(phone: str, name: str) -> tuple[str, int]:
    resp = request("POST", "/auth/login", {"phone": phone, "name": name})
    return resp["token"], resp["user"]["id"]


def main() -> None:
    print(f"==> 目标服务: {BASE_URL}\n")

    # 1. 登录两个用户
    print("[1/7] 登录两个用户...")
    token_a, uid_a = login("13800000001", "小明")
    token_b, uid_b = login("13800000002", "小红")
    print(f"      小明 id={uid_a}，小红 id={uid_b}\n")

    # 2. 小明创建 trip
    print("[2/7] 小明创建 trip...")
    trip = request("POST", "/trips", {"title": "周六火锅局", "default_mode": "transit"}, token_a)
    trip_id = trip["trip_id"]
    invite_code = trip["invite_code"]
    my_trips_a = request("GET", "/trips", token=token_a)["trips"]
    if not any(item["trip_id"] == trip_id and item["role"] == "creator" for item in my_trips_a):
        raise RuntimeError("创建后的 trip 未出现在发起人的我的 trip 列表")
    print(f"      trip_id={trip_id}，邀请码={invite_code}\n")

    # 3. 小红加入
    print("[3/7] 小红用邀请码加入...")
    joined = request("POST", f"/trips/{trip_id}/join", {"invite_code": invite_code}, token_b)
    my_trips_b = request("GET", "/trips", token=token_b)["trips"]
    if not any(item["trip_id"] == trip_id and item["role"] == "member" for item in my_trips_b):
        raise RuntimeError("加入后的 trip 未出现在成员的我的 trip 列表")
    print(f"      participant_id={joined['participant_id']}，role={joined['role']}\n")

    # Trip 主方案类型只有创建者可切换，并对所有成员可见。
    expect_http_error(
        403, "PUT", f"/trips/{trip_id}/workflow", {"primary_workflow": "meeting"}, token_b
    )
    request("PUT", f"/trips/{trip_id}/workflow", {"primary_workflow": "meeting"}, token_a)
    if request("GET", f"/trips/{trip_id}", token=token_b)["primary_workflow"] != "meeting":
        raise RuntimeError("Trip 主方案类型未同步给参与者")
    request("PUT", f"/trips/{trip_id}/workflow", {"primary_workflow": "itinerary"}, token_a)

    # Phase 2A：双方添加地点，发起人确认状态，成员投票。
    destination_a = request(
        "POST", f"/trips/{trip_id}/destinations",
        {"name": "故宫", "address": "北京市东城区", "lat": 39.9163, "lng": 116.3972,
         "category": "博物馆", "expected_stay_min": 120}, token_a,
    )
    destination_b = request(
        "POST", f"/trips/{trip_id}/destinations",
        {"name": "什刹海", "address": "北京市西城区", "lat": 39.9416, "lng": 116.3852,
         "category": "景点", "expected_stay_min": 60}, token_b,
    )
    request(
        "PUT", f"/trips/{trip_id}/destinations/{destination_a['id']}/status",
        {"visit_status": "must_visit"}, token_a,
    )
    expect_http_error(
        403, "PUT", f"/trips/{trip_id}/destinations/{destination_b['id']}/status",
        {"visit_status": "optional"}, token_b,
    )
    request(
        "PUT", f"/trips/{trip_id}/destinations/{destination_b['id']}/status",
        {"visit_status": "optional"}, token_a,
    )
    request(
        "POST", f"/trips/{trip_id}/votes",
        {"candidate_type": "destination", "candidate_id": str(destination_a["id"]), "vote_value": 1},
        token_b,
    )
    destination_list = request("GET", f"/trips/{trip_id}/destinations", token=token_b)
    listed_a = next(d for d in destination_list["destinations"] if d["id"] == destination_a["id"])
    if listed_a["visit_status"] != "must_visit" or listed_a["votes"]["up"] != 1:
        raise RuntimeError(f"Phase 2A 地点状态或投票不正确：{listed_a}")
    print("      Phase 2A 地点协作接口验证通过\n")

    # 4. 两人各自填出发点 + 出行方式
    print("[4/7] 两人填出发点 + 出行方式...")
    request(
        "PUT",
        f"/trips/{trip_id}/participants/me",
        {
            "start_location": {"lat": 39.999, "lng": 116.481, "address": "望京SOHO"},
            "transport_mode": "driving",
            "available_from": "2026-09-05T14:10:00+08:00",
            "available_until": "2026-09-05T19:00:00+08:00",
        },
        token_a,
    )
    request(
        "PUT",
        f"/trips/{trip_id}/participants/me",
        {
            "start_location": {"lat": 39.908, "lng": 116.446, "address": "国贸大厦"},
            "transport_mode": "transit",
            "available_from": "2026-09-05T14:00:00+08:00",
            "available_until": "2026-09-05T18:00:00+08:00",
        },
        token_b,
    )
    print("      已提交\n")
    participant_snapshot = request("GET", f"/trips/{trip_id}/participants", token=token_a)["participants"]
    if not all(item.get("available_from") and item.get("available_until") for item in participant_snapshot):
        raise RuntimeError("参与者个人可用时间未正确保存")

    # Phase 2B：保存时间/优化目标并生成覆盖全部确认地点的路线。
    request(
        "PUT", f"/trips/{trip_id}/planning-settings",
        {
            "planned_start_at": "2026-09-05T14:00:00+08:00",
            "planned_end_at": "2026-09-05T20:00:00+08:00",
            "group_transport_mode": "transit",
            "optimization_objective": "total_time",
        }, token_a,
    )
    itinerary = request("POST", f"/trips/{trip_id}/itinerary-plans", {}, token_a)
    planned_ids = {stop["destination_id"] for stop in itinerary["ordered_stops"]}
    if planned_ids != {destination_a["id"], destination_b["id"]}:
        raise RuntimeError(f"Phase 2B 未覆盖全部确认地点：{planned_ids}")
    confirmed = request(
        "POST", f"/trips/{trip_id}/itinerary-plans/{itinerary['plan_id']}/confirm",
        {"accept_estimated_routes": True}, token_a
    )
    if confirmed["status"] != "confirmed":
        raise RuntimeError(f"Phase 2C 方案确认失败：{confirmed}")
    expect_http_error(
        409, "PUT", f"/trips/{trip_id}/planning-settings",
        {
            "planned_start_at": "2026-09-05T14:00:00+08:00",
            "planned_end_at": "2026-09-05T20:00:00+08:00",
            "group_transport_mode": "transit", "optimization_objective": "distance",
        }, token_a,
    )
    request("POST", f"/trips/{trip_id}/reopen", {}, token_a)
    request(
        "PUT", f"/trips/{trip_id}/planning-settings",
        {
            "planned_start_at": "2026-09-05T14:00:00+08:00",
            "planned_end_at": "2026-09-05T20:00:00+08:00",
            "group_transport_mode": "transit", "optimization_objective": "distance",
        }, token_a,
    )
    expect_http_error(
        409, "POST", f"/trips/{trip_id}/itinerary-plans/{itinerary['plan_id']}/confirm",
        {"accept_estimated_routes": True}, token_a
    )
    print("      Phase 2B 第一站、完整地点顺序和时间线验证通过\n")

    # Phase 3：解析分享文本、地理编码并确认加入候选地点。
    print("[5/7] 解析分享文本并加入地点...")
    parsed = request(
        "POST", f"/trips/{trip_id}/shared-text/parse",
        {"text": "名称：天坛公园\n地址：北京市东城区天坛路甲1号\n品类：景点\n推荐理由：古建筑",
         "default_stay_min": 90}, token_a,
    )
    if not parsed["candidates"]:
        raise RuntimeError(f"Phase 3 未能提取并定位地点：{parsed}")
    imported = parsed["candidates"][0]
    if imported.get("lat") is None:
        resolution_error = str(imported.get("resolution_error") or "")
        if any(marker in resolution_error for marker in ("10021", "CUQPS_HAS_EXCEEDED_THE_LIMIT", "配额")):
            imported = {
                **imported, "lat": 39.8822, "lng": 116.4066,
                "resolution_source": "e2e_quota_fallback",
            }
            print("      高德配额不可用：地点提取继续验证，真实定位标记为外部服务跳过")
        else:
            raise RuntimeError(f"Phase 3 未能定位地点：{parsed}")
    imported_destination = request(
        "POST", f"/trips/{trip_id}/destinations",
        {"name": imported["name"], "address": imported["address"],
         "lat": imported["lat"], "lng": imported["lng"],
         "category": imported["category"] or None,
         "expected_stay_min": imported["expected_stay_min"],
         "note": imported["reason"] or None}, token_a,
    )
    parse_feedback = request(
        "POST", f"/trips/{trip_id}/place-parse-feedback",
        {
            "parse_session_id": parsed["parse_session_id"],
            "candidate_id": imported["parse_candidate_id"],
            "parser": parsed["parser"], "action": "accepted",
            "proposed_place": {
                "name": imported["name"], "address": imported.get("address") or None,
                "city": imported.get("city") or None, "category": imported.get("category") or None,
                "lat": imported.get("lat"), "lng": imported.get("lng"),
                "expected_stay_min": imported.get("expected_stay_min"),
                "resolution_source": imported.get("resolution_source"),
            },
            "final_place": {
                "name": imported_destination["name"], "address": imported_destination.get("address"),
                "category": imported_destination.get("category"),
                "lat": imported_destination["lat"], "lng": imported_destination["lng"],
                "expected_stay_min": imported_destination["expected_stay_min"],
                "resolution_source": imported.get("resolution_source"),
            },
            "consent_to_improve": False,
        }, token_a,
    )
    if not parse_feedback.get("recorded") or parse_feedback.get("action") != "accepted":
        raise RuntimeError(f"Phase 4A-3 解析反馈未记录：{parse_feedback}")
    expect_http_error(
        422, "POST", f"/trips/{trip_id}/place-parse-feedback",
        {
            "parse_session_id": parsed["parse_session_id"],
            "candidate_id": imported["parse_candidate_id"],
            "parser": parsed["parser"], "action": "accepted",
            "proposed_place": {"name": imported["name"]},
            "final_place": {"name": imported_destination["name"]},
            "consent_to_improve": True,
        }, token_a,
    )
    current_destinations = request("GET", f"/trips/{trip_id}/destinations", token=token_a)["destinations"]
    if not any(item["id"] == imported_destination["id"] for item in current_destinations):
        raise RuntimeError("Phase 3 确认后的地点未出现在 trip 地点列表")
    print("      Phase 3 文本提取、地理编码和确认加入验证通过\n")

    # 6. 触发约点推荐
    print("[6/7] 触发约点推荐（minimax 公平优先）...")
    request("PUT", f"/trips/{trip_id}/workflow", {"primary_workflow": "meeting"}, token_a)
    result = request("POST", f"/trips/{trip_id}/meeting-points", {"objective": "minimax"}, token_a)
    candidates = result["candidates"]
    print(f"      得到 {len(candidates)} 个候选点\n")

    # 6. 打印结果
    print("[7/7] 推荐结果：\n")
    for c in candidates:
        poi = c["poi"]
        print(f"  排名 {c['rank']} | {poi['name']}")
        print(f"    地址: {poi['address'] or '(网格采样点)'}")
        print(f"    指标: 最长通勤 {c['metrics']['max_travel_time_min']} 分钟 | 总耗时 {c['metrics']['total_travel_time_min']} 分钟")
        for pp in c["per_person"]:
            line = f"      - {pp['name']}({pp['mode']}): {pp['travel_time_min']} 分钟 | {pp['route_summary']}"
            print(line)
            if pp.get("parking"):
                p = pp["parking"]
                lots = "、".join(l["name"] for l in p.get("lots", [])) or "无"
                print(f"        停车: {p.get('difficulty', '')} | 附近停车场: {lots}")
        print()

    # 两名参与者对同一候选点投票，并验证聚合、个人选择和取消投票。
    candidate_id = candidates[0]["candidate_id"]
    request(
        "POST", f"/trips/{trip_id}/votes",
        {"candidate_type": "meeting_point", "candidate_id": candidate_id, "vote_value": 1}, token_a,
    )
    request(
        "POST", f"/trips/{trip_id}/votes",
        {"candidate_type": "meeting_point", "candidate_id": candidate_id, "vote_value": -1}, token_b,
    )
    votes_a = request("GET", f"/trips/{trip_id}/votes", token=token_a)["results"]
    vote_a = next(v for v in votes_a if v["candidate_id"] == candidate_id)
    if (vote_a["up"], vote_a["down"], vote_a["my_vote"]) != (1, 1, 1):
        raise RuntimeError(f"投票聚合结果不正确：{vote_a}")
    request(
        "POST", f"/trips/{trip_id}/votes",
        {"candidate_type": "meeting_point", "candidate_id": candidate_id, "vote_value": 0}, token_b,
    )
    votes_b = request("GET", f"/trips/{trip_id}/votes", token=token_b)["results"]
    vote_b = next(v for v in votes_b if v["candidate_id"] == candidate_id)
    if (vote_b["up"], vote_b["down"], vote_b["my_vote"]) != (1, 0, 0):
        raise RuntimeError(f"取消投票结果不正确：{vote_b}")
    print("投票、聚合与取消投票验证通过")

    # 权限与输入校验：未加入者不能读取 trip，非法票值应被拒绝。
    token_c, _ = login("13800000003", "未加入用户")
    expect_http_error(403, "GET", f"/trips/{trip_id}", token=token_c)
    expect_http_error(
        422,
        "POST",
        f"/trips/{trip_id}/votes",
        {"candidate_type": "meeting_point", "candidate_id": "candidate-1", "vote_value": 2},
        token_a,
    )
    print("权限与投票输入校验通过")

    # Phase 4A-1：确认、完成、历史筛选和完成后永久只读。
    expect_http_error(
        409, "PUT", f"/trips/{trip_id}/destinations/{destination_a['id']}/feedback/me",
        {"visited": True, "rating": 5, "tags": []}, token_a,
    )
    request("PUT", f"/trips/{trip_id}/workflow", {"primary_workflow": "itinerary"}, token_a)
    final_plan = request("POST", f"/trips/{trip_id}/itinerary-plans", {}, token_a)
    request(
        "POST", f"/trips/{trip_id}/itinerary-plans/{final_plan['plan_id']}/confirm",
        {"accept_estimated_routes": True}, token_a
    )
    expect_http_error(403, "POST", f"/trips/{trip_id}/complete", {}, token_b)
    completed = request("POST", f"/trips/{trip_id}/complete", {}, token_a)
    if completed["status"] != "completed" or not completed.get("completed_at"):
        raise RuntimeError(f"Phase 4A-1 完成状态写入失败：{completed}")
    expect_http_error(409, "POST", f"/trips/{trip_id}/reopen", {}, token_a)
    expect_http_error(
        409, "PUT", f"/trips/{trip_id}/participants/me",
        {
            "start_location": {"lat": 39.9, "lng": 116.4, "address": "不可修改"},
            "transport_mode": "transit",
        }, token_a,
    )
    completed_trips = request("GET", "/trips?status=completed", token=token_a)["trips"]
    if not any(item["trip_id"] == trip_id for item in completed_trips):
        raise RuntimeError("Phase 4A-1 已完成 Trip 未出现在状态筛选结果中")
    print("Phase 4A-1 生命周期、权限、筛选和永久只读验证通过")

    # Phase 4A-2：成员只能在完成后评价本人反馈，列表只返回聚合与本人的文字。
    member_feedback = request(
        "PUT", f"/trips/{trip_id}/destinations/{destination_a['id']}/feedback/me",
        {
            "visited": True, "rating": 4, "actual_stay_min": 100,
            "tags": ["交通方便"], "comment": "成员私密评价", "would_revisit": True,
        }, token_b,
    )
    if member_feedback["my_feedback"]["rating"] != 4:
        raise RuntimeError("Phase 4A-2 成员反馈保存失败")
    request(
        "PUT", f"/trips/{trip_id}/destinations/{destination_a['id']}/feedback/me",
        {
            "visited": True, "rating": 5, "actual_stay_min": 120,
            "tags": ["值得再去", "交通方便"], "comment": "创建者私密评价", "would_revisit": True,
        }, token_a,
    )
    for destination_id, rating, stay in (
        (destination_b["id"], 3, 70), (imported_destination["id"], 4, 95),
    ):
        request(
            "PUT", f"/trips/{trip_id}/destinations/{destination_id}/feedback/me",
            {
                "visited": True, "rating": rating, "actual_stay_min": stay,
                "tags": [], "comment": None, "would_revisit": True,
            }, token_a,
        )
    feedback = request("GET", f"/trips/{trip_id}/feedback", token=token_b)
    destination_feedback = next(
        item for item in feedback["destinations"] if item["destination_id"] == destination_a["id"]
    )
    if destination_feedback["summary"]["average_rating"] != 4.5:
        raise RuntimeError(f"Phase 4A-2 评分聚合错误：{destination_feedback}")
    if destination_feedback["my_feedback"]["comment"] != "成员私密评价":
        raise RuntimeError("Phase 4A-2 未正确返回本人评价")
    if "创建者私密评价" in str(destination_feedback):
        raise RuntimeError("Phase 4A-2 泄露了其他成员的文字评价")
    print("Phase 4A-2 地点评价、聚合与文字隐私验证通过")

    # Phase 4B：只从本人已完成行程反馈重建偏好，并可关闭、清空。
    current_preferences = request("GET", "/users/me/preferences", token=token_a)
    request(
        "PUT", "/users/me/preferences",
        {
            "personalization_enabled": True,
            "explicit": {
                **current_preferences["explicit"],
                "preferred_categories": ["博物馆"],
                "preferred_transport_modes": ["transit"],
            },
        }, token_a,
    )
    preferences = request("POST", "/users/me/preferences/rebuild", {}, token_a)
    if preferences["derived"]["sample_count"] < 3 or not preferences["derived"]["recommendation_ready"]:
        raise RuntimeError(f"Phase 4B 样本阈值或本人反馈统计错误：{preferences}")
    personalized = request("GET", f"/trips/{trip_id}/destinations", token=token_a)["destinations"]
    museum = next(item for item in personalized if item["id"] == destination_a["id"])
    recommendation = museum.get("recommendation") or {}
    if not recommendation.get("active") or recommendation.get("preference_adjustment", 0) <= 0:
        raise RuntimeError(f"Phase 4B 个性化软分数未生效：{recommendation}")
    if not recommendation.get("reasons"):
        raise RuntimeError("Phase 4B 个性化推荐缺少可解释原因")
    if not any("主动选择" in reason for reason in recommendation["reasons"]):
        raise RuntimeError(f"Phase 4B 显式偏好未进入推荐解释：{recommendation}")
    emptied_preferences = request(
        "PUT", "/users/me/preferences",
        {
            "personalization_enabled": True,
            "explicit": {
                "preferred_categories": [], "preferred_transport_modes": [], "avoid_tags": [],
            },
        }, token_a,
    )
    if any(emptied_preferences["explicit"].values()):
        raise RuntimeError(f"Phase 4B 空列表未能清除主动偏好：{emptied_preferences}")
    persisted_empty = request("GET", "/users/me/preferences", token=token_a)
    if any(persisted_empty["explicit"].values()):
        raise RuntimeError(f"Phase 4B 清空主动偏好后重新读取仍有旧值：{persisted_empty}")
    preferences = request(
        "PUT", "/users/me/preferences",
        {"personalization_enabled": False, "explicit": preferences["explicit"]}, token_a,
    )
    if preferences["personalization_enabled"]:
        raise RuntimeError("Phase 4B 个性化关闭失败")
    cleared = request("DELETE", "/users/me/preferences/derived", token=token_a)
    if cleared["derived"]["sample_count"] != 0:
        raise RuntimeError("Phase 4B 派生偏好清空失败")
    print("Phase 4B 偏好重建、软分数解释、关闭和清空验证通过")

    # Phase 4 数据治理：删除本次测试 Trip，并由数据库外键级联清理评价与解析反馈。
    request("DELETE", f"/trips/{trip_id}", token=token_a)
    expect_http_error(404, "GET", f"/trips/{trip_id}", token=token_a)
    print("Phase 4 Trip 删除与关联数据级联入口验证通过")

    # Windows 默认 GBK 控制台无法编码部分 Unicode 符号，保持输出可跨平台执行。
    print("==> 闭环跑通 [OK]")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"\n[FAIL] 测试失败: {e}")
        sys.exit(1)
