"""One-command disposable local demo; fixed synthetic data, no external services."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack, suppress
from datetime import datetime, timedelta, timezone
import html
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def configure_environment():
    # Called only by the dedicated process, never at module import in tests.
    for key in ("AMAP_API_KEY", "AMAP_JS_KEY", "AMAP_JS_SECURITY_CODE", "LLM_API_KEY", "LLM_MODEL"):
        os.environ[key] = ""
    os.environ.update(APP_ENV="development", DEBUG="false", SMS_PROVIDER="disabled",
                      ALIYUN_SMS_ENABLED="false", AUTH_COOKIE_SECURE="false",
                      AUTH_SECRET=secrets.token_hex(32), AGENT_DB_TESTS="1")


def write_state(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


async def seed_demo(db, password):
    from app.models import PasswordCredential, SavedPlace, Trip, TripDestination, TripParticipant, User
    from app.services.password_auth import hash_password
    people = [User(name="演示甲", preferences={"personalization_enabled": True,
               "explicit": {"preferred_categories": ["公园"], "avoid_tags": []}}),
              User(name="演示乙", preferences={"personalization_enabled": True,
               "explicit": {"preferred_categories": ["清净"], "avoid_tags": []}})]
    db.add_all(people)
    await db.flush()
    for index, person in enumerate(people, 1):
        db.add(PasswordCredential(user_id=person.id, username=f"demo_{index}", password_hash=hash_password(password)))
    start = datetime(2026, 11, 8, 5, tzinfo=timezone.utc)
    trips = []
    for index, title in enumerate(("演示1·多人吃饭", "演示2·寿司郎分店", "演示3·公园半日游", "演示4·时间冲突修正"), 1):
        trip = Trip(title=title, creator_id=people[0].id, invite_code=f"DEMO0{index}",
                    planned_start_at=start, planned_end_at=start + timedelta(hours=1 if index == 4 else 5),
                    return_location={"lat": 31.22, "lng": 121.42, "address": "演示公园入口"} if index == 3 else None)
        db.add(trip)
        await db.flush()
        for number, person in enumerate(people):
            db.add(TripParticipant(trip_id=trip.id, user_id=person.id, role="creator" if number == 0 else "member",
                                  start_location={"lat": 31.22, "lng": 121.40 + number * .04, "address": "合成出发点"},
                                  transport_mode="transit", share_preferences=True))
        if index >= 3:
            for number, (name, category) in enumerate((("演示湖畔公园", "公园"), ("演示美术馆", "博物馆"))):
                db.add(TripDestination(trip_id=trip.id, submitted_by=people[0].id, name=name, category=category,
                                      lat=31.22, lng=121.42 + number * .002, expected_stay_min=90 if index == 4 else 30,
                                      visit_status="must_visit"))
        trips.append({"id": trip.id, "title": title, "invite_code": trip.invite_code})
    for index, (name, category, tags) in enumerate((
        ("演示湖畔公园", "公园", ["周末", "散步"]), ("演示美术馆", "博物馆", ["雨天", "周末"]),
        ("演示咖啡店", "餐饮", ["雨天"]), ("寿司郎演示商场店", "餐饮", ["聚餐"]),
        ("寿司郎演示公园店", "餐饮", ["聚餐", "散步"]))):
        db.add(SavedPlace(user_id=people[0].id, place_key=f"fixed_demo_place_{index}", name=name, category=category,
                          city="上海市", address="合成演示地址", lat=31.22, lng=121.42 + index * .002,
                          tags=tags, private_note="仅演示甲可见的合成回忆，同行不能查看", expected_stay_min=30))
    await db.commit()
    return trips


def demo_guide(trips):
    items = "".join(f"<li>{html.escape(t['title'])}（ID {t['id']}，邀请码 {html.escape(t['invite_code'])}）</li>" for t in trips)
    return """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>本机合成演示</title>
<style>body{font:18px/1.8 system-ui;max-width:850px;margin:40px auto;padding:20px;background:#fffaf0;color:#31443c}a{color:#236b54}code{background:#eee;padding:3px}li{margin:8px 0}</style>
<h1>本机合成演示</h1><p>所有地点与道路结果均为演示夹具。模型使用现有规则降级；地图未加载，路线是明确标注的估算。</p>
<p><a href="/">打开应用并登录</a> · <a href="/docs">API文档</a> · <a href="/ready">数据库就绪检查</a></p>
<p>账号 demo_1 为发起人，demo_2 为同行。随机短期密码仅在本次运行目录的 credentials.json 中，不在网页提供。</p><ol>""" + items + """</ol>
<ol><li>使用demo_1登录，打开演示1，在行程助手选择多人吃饭，城市填上海市，查看赴约耗时与估算说明。</li>
<li>演示2选择品牌分店，品牌填寿司郎，查看商场/公园线索及清净程度未知的说明。</li>
<li>演示3选择附近半日游，锚点填演示公园入口，使用已有日期时间，选择返回中心；预览后再确认加入。</li>
<li>演示3在助手输入“生成当前路线”，先查看草案，再确认执行；重复确认可用API验证回执重放。最终确认需要明确接受估算。</li>
<li>演示4生成路线查看时间冲突，把两处停留改为15分钟后重算，比较前后方案。</li>
<li>开发者工具Network查看聊天/执行响应的trace_id，再打开对应 /trips/ID/assistant/traces/TRACE_ID。demo_2可见同行行程及评论，看不到demo_1的收藏或trace。</li></ol>
<p>退出终端Ctrl+C或创建本次运行目录中的stop文件，正常退出清理数据。重新启动恢复初始演示。详细脚本见scripts/LOCAL_DEMO.md。</p></html>"""


async def run(args):
    if not 5 <= args.ttl <= 7200:
        raise ValueError("TTL must be between 5 and 7200 seconds")
    directory = args.run_dir or ROOT / "docs_private/demo_runs" / (datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid4().hex[:8])
    if directory.is_symlink():
        raise FileExistsError("Run directory must not be a symlink")
    directory = directory.resolve()
    # Refuse to overwrite any previous run or follow an existing run-directory symlink.
    directory.mkdir(parents=True, exist_ok=False)
    state_file = directory / "state.json"
    credentials_file = directory / "credentials.json"
    stop_file = directory / "stop"
    configure_environment()
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    import uvicorn
    import httpx
    from fastapi.responses import HTMLResponse, JSONResponse
    from app.config import settings
    from app.db import Base, get_db
    import app.main as main
    from app.routers import ws
    from app.services import agent_state
    from app.services.agent_trace import _prune
    from app.services.amap import amap
    from app.services.llm_parser import llm_place_parser
    from unittest.mock import patch

    schema = "agent_demo_" + uuid4().hex
    password = secrets.token_urlsafe(9)
    url = os.getenv("TEST_DATABASE_URL", settings.database_url)
    admin = create_async_engine(url, echo=False, hide_parameters=True)
    engine = None
    created = False
    sock = None
    server = None
    serving = None
    stack = ExitStack()
    write_state(state_file, {"stage": "starting", "schema": schema})
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        created = True
        engine = create_async_engine(url, echo=False, hide_parameters=True,
                                     connect_args={"server_settings": {"search_path": schema}})
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async def isolated_db():
            async with sessions() as db:
                yield db
        previous_overrides = dict(main.app.dependency_overrides)
        main.app.dependency_overrides[get_db] = isolated_db
        stack.callback(lambda: (main.app.dependency_overrides.clear(), main.app.dependency_overrides.update(previous_overrides)))
        stack.enter_context(patch.object(main, "engine", engine))
        stack.enter_context(patch.object(ws, "SessionLocal", sessions))
        stack.enter_context(patch.object(agent_state, "SessionLocal", sessions))
        async with sessions() as db:
            trips = await seed_demo(db, password)

        def place(name, lng, category="餐饮", **values):
            return {"name": name, "lat": 31.22, "lng": lng, "category": category, "city": "上海市",
                    "address": "合成演示地址", "poi_id": "demo-" + name, **values}
        async def unavailable(*args, **kwargs):
            amap.last_error = "本机合成演示：未调用道路服务，使用不可导航的估算"
            return None
        async def search(keyword, city, limit=10):
            if city != "上海市":
                return []
            if keyword == "寿司郎":
                return [place("寿司郎演示商场店", 121.426, address="合成演示购物中心"),
                        place("寿司郎演示公园店", 121.428)][:limit]
            known = {"演示公园入口": place("演示公园入口", 121.42, "公园"),
                     "演示湖畔公园": place("演示湖畔公园", 121.42, "公园"),
                     "演示美术馆": place("演示美术馆", 121.422, "博物馆")}
            return [known[keyword]] if keyword in known else []
        async def nearby(location, **kwargs):
            if kwargs.get("types") == "050000":
                return [place("演示中点餐厅", 121.42), place("演示河畔餐厅", 121.428)]
            return [place("演示湖畔公园", 121.42, "公园"), place("演示美术馆", 121.422, "博物馆")]
        async def geocode(address):
            if address in {"演示公园入口", "演示湖畔公园", "演示美术馆"}:
                return {"lat": 31.22, "lng": 121.422 if address == "演示美术馆" else 121.42}
            return None
        async def blocked_send(*args, **kwargs):
            raise RuntimeError("External HTTP is disabled in the demo process")
        for name, value in (("available", True), ("_get", unavailable), ("route_detail", unavailable),
                            ("search_pois", search), ("nearby_pois", nearby), ("geocode", geocode)):
            stack.enter_context(patch.object(amap, name, value))
        stack.enter_context(patch.object(httpx.AsyncClient, "send", blocked_send))

        guide = demo_guide(trips)
        @main.app.middleware("http")
        async def demo_label(request, call_next):
            if request.url.path == "/demo":
                response = HTMLResponse(guide)
            elif request.url.path == "/demo/info":
                response = JSONResponse({"synthetic": True, "external_http": False, "trips": trips})
            elif request.url.path == "/":
                source = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
                banner = '<div style="position:fixed;bottom:0;left:0;right:0;z-index:99999;text-align:center;background:#fff3c8;color:#294536;font:13px/26px system-ui">合成演示 · 模型规则降级 · 路线估算 · <a href="/demo">演示指南</a></div>'
                response = HTMLResponse(source.replace("</body>", banner + "</body>"))
            else:
                response = await call_next(request)
            response.headers["X-Demo-Fixture"] = "synthetic-isolated"
            response.headers["Cache-Control"] = "no-store"
            return response

        sock = socket.socket()
        sock.bind(("127.0.0.2", 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(main.app, lifespan="off", log_level="critical", access_log=False))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        startup_deadline = asyncio.get_running_loop().time() + 15
        while not server.started:
            if serving.done():
                await serving
                raise RuntimeError("Server exited before readiness")
            if asyncio.get_running_loop().time() > startup_deadline:
                raise TimeoutError("Demo startup timed out")
            await asyncio.sleep(.05)
        write_state(credentials_file, {"users": ["demo_1", "demo_2"], "password": password})
        write_state(state_file, {"stage": "ready", "schema": schema, "pid": os.getpid(), "host": "127.0.0.2",
                                 "port": port, "url": f"http://127.0.0.2:{port}/demo", "trips": trips})
        print(f"Demo: http://127.0.0.2:{port}/demo\nCredentials: {credentials_file}\nStop: Ctrl+C or create {stop_file}", flush=True)
        deadline = asyncio.get_running_loop().time() + args.ttl
        while not stop_file.exists() and asyncio.get_running_loop().time() < deadline:
            if serving.done():
                await serving
                break
            _prune()
            async with sessions() as db:
                await agent_state.cleanup_agent_data(db)
                await db.commit()
            await asyncio.sleep(.5)
    finally:
        if server:
            server.should_exit = True
        if serving:
            try:
                await asyncio.wait_for(asyncio.shield(serving), timeout=20)
            except (TimeoutError, asyncio.CancelledError):
                serving.cancel()
                with suppress(asyncio.CancelledError):
                    await serving
        if sock:
            sock.close()
        stack.close()
        await amap.aclose()
        await llm_place_parser.aclose()
        if engine:
            await engine.dispose()
        # Remove short-lived credentials even when DB cleanup fails.
        credentials_file.unlink(missing_ok=True)
        try:
            if created:
                assert re.fullmatch(r"agent_demo_[0-9a-f]{32}", schema)
                async with admin.begin() as conn:
                    await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            write_state(state_file, {"stage": "cleaned", "schema": schema})
        except Exception:
            write_state(state_file, {"stage": "cleanup_failed", "schema": schema})
            raise
        finally:
            await admin.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, help="New private directory; existing paths are rejected")
    parser.add_argument("--ttl", type=int, default=3600, help="Automatic stop after 5..7200 seconds")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print("Demo failed: " + type(exc).__name__ + "; inspect private state.json", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
