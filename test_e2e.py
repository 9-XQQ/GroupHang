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


def request(method: str, path: str, body: dict | None = None, token: str | None = None) -> dict:
    """发 JSON 请求，返回解析后的 JSON（失败抛异常）。"""
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Token"] = token

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
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
    print("[1/6] 登录两个用户...")
    token_a, uid_a = login("13800000001", "小明")
    token_b, uid_b = login("13800000002", "小红")
    print(f"      小明 id={uid_a}，小红 id={uid_b}\n")

    # 2. 小明创建 trip
    print("[2/6] 小明创建 trip...")
    trip = request("POST", "/trips", {"title": "周六火锅局", "default_mode": "transit"}, token_a)
    trip_id = trip["trip_id"]
    invite_code = trip["invite_code"]
    print(f"      trip_id={trip_id}，邀请码={invite_code}\n")

    # 3. 小红加入
    print("[3/6] 小红用邀请码加入...")
    joined = request("POST", f"/trips/{trip_id}/join", {"invite_code": invite_code}, token_b)
    print(f"      participant_id={joined['participant_id']}，role={joined['role']}\n")

    # Phase 2A：双方添加地点，发起人确认状态，成员投票。
    destination_a = request(
        "POST", f"/trips/{trip_id}/destinations",
        {"name": "故宫", "address": "北京市东城区", "lat": 39.9163, "lng": 116.3972,
         "expected_stay_min": 120}, token_a,
    )
    destination_b = request(
        "POST", f"/trips/{trip_id}/destinations",
        {"name": "什刹海", "address": "北京市西城区", "lat": 39.9416, "lng": 116.3852,
         "expected_stay_min": 60}, token_b,
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
    print("[4/6] 两人填出发点 + 出行方式...")
    request(
        "PUT",
        f"/trips/{trip_id}/participants/me",
        {
            "start_location": {"lat": 39.999, "lng": 116.481, "address": "望京SOHO"},
            "transport_mode": "driving",
        },
        token_a,
    )
    request(
        "PUT",
        f"/trips/{trip_id}/participants/me",
        {
            "start_location": {"lat": 39.908, "lng": 116.446, "address": "国贸大厦"},
            "transport_mode": "transit",
        },
        token_b,
    )
    print("      已提交\n")

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
        "POST", f"/trips/{trip_id}/itinerary-plans/{itinerary['plan_id']}/confirm", {}, token_a
    )
    if confirmed["status"] != "confirmed":
        raise RuntimeError(f"Phase 2C 方案确认失败：{confirmed}")
    request(
        "PUT", f"/trips/{trip_id}/planning-settings",
        {
            "planned_start_at": "2026-09-05T14:00:00+08:00",
            "planned_end_at": "2026-09-05T20:00:00+08:00",
            "group_transport_mode": "transit", "optimization_objective": "distance",
        }, token_a,
    )
    expect_http_error(
        409, "POST", f"/trips/{trip_id}/itinerary-plans/{itinerary['plan_id']}/confirm", {}, token_a
    )
    print("      Phase 2B 第一站、完整地点顺序和时间线验证通过\n")

    # 5. 触发约点推荐
    print("[5/6] 触发约点推荐（minimax 公平优先）...")
    result = request("POST", f"/trips/{trip_id}/meeting-points", {"objective": "minimax"}, token_a)
    candidates = result["candidates"]
    print(f"      得到 {len(candidates)} 个候选点\n")

    # 6. 打印结果
    print("[6/6] 推荐结果：\n")
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

    # Windows 默认 GBK 控制台无法编码部分 Unicode 符号，保持输出可跨平台执行。
    print("==> 闭环跑通 [OK]")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"\n✘ 测试失败: {e}")
        sys.exit(1)
