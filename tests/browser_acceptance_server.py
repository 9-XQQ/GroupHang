"""Disposable browser acceptance server. No production users or external APIs.

AGENT_DB_TESTS=1 python -m tests.browser_acceptance_server --state-file ... --stop-file ...
The private state file holds random disposable credentials only while running.
"""
import argparse
import asyncio
import json
import os
import secrets
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

# Set these before importing clients/settings; never edit the user's .env.
for key in ("AMAP_API_KEY", "AMAP_JS_KEY", "AMAP_JS_SECURITY_CODE", "LLM_API_KEY"):
    os.environ[key] = ""
os.environ.update(APP_ENV="development", DEBUG="false", SMS_PROVIDER="disabled",
                  ALIYUN_SMS_ENABLED="false", AUTH_COOKIE_SECURE="false",
                  AUTH_SECRET=secrets.token_hex(32))


async def run(args):
    if os.getenv("AGENT_DB_TESTS") != "1":
        raise RuntimeError("Explicit AGENT_DB_TESTS=1 is required")
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    import uvicorn
    from app.config import settings
    from app.db import Base, get_db
    from app.main import app
    from app.models import ItineraryPlan, PasswordCredential, SavedPlace, Trip, TripDestination, TripParticipant, User
    from app.routers import ws
    from app.services.amap import amap
    from app.services.llm_parser import llm_place_parser
    from app.services.password_auth import hash_password

    schema = "agent_test_" + uuid4().hex
    admin = create_async_engine(os.getenv("TEST_DATABASE_URL", settings.database_url))
    engine = None
    sock = None
    created = False
    server = None
    password = secrets.token_urlsafe(9)
    try:
        async with admin.begin() as connection:
            await connection.execute(text('CREATE SCHEMA "' + schema + '"'))
        created = True
        engine = create_async_engine(os.getenv("TEST_DATABASE_URL", settings.database_url),
                                     connect_args={"server_settings": {"search_path": schema}})
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async def isolated_db():
            async with sessions() as db:
                yield db
        app.dependency_overrides[get_db] = isolated_db
        # WebSocket uses its own imported factory rather than get_db.
        ws.SessionLocal = sessions
        async with sessions() as db:
            people = [User(name=name) for name in ("验收甲", "验收乙", "验收外部用户")]
            db.add_all(people)
            await db.flush()
            for number, user in enumerate(people):
                db.add(PasswordCredential(user_id=user.id, username="acceptance_" + str(number + 1),
                                          password_hash=hash_password(password)))
            trip = Trip(title="隔离验收·既有行程", creator_id=people[0].id, invite_code="UITEST",
                        planned_start_at=datetime(2026, 11, 1, 5, tzinfo=timezone.utc),
                        planned_end_at=datetime(2026, 11, 1, 10, tzinfo=timezone.utc))
            db.add(trip)
            await db.flush()
            for number, user in enumerate(people[:2]):
                db.add(TripParticipant(trip_id=trip.id, user_id=user.id,
                                      role="creator" if number == 0 else "member",
                                      start_location={"lat":31.20+number*.01,"lng":121.40+number*.01},
                                      transport_mode="transit"))
            for number, (name, category) in enumerate([
                ("验收湖畔公园","公园"),("验收美术馆","博物馆"),("验收咖啡店","餐饮"),
                ("寿司郎验收商场店","餐饮"),("寿司郎验收公园店","餐饮")]):
                db.add(SavedPlace(user_id=people[0].id, place_key=secrets.token_hex(32),
                                  name=name, category=category, city="上海市",
                                  address="隔离验收示例地址", lat=31.22+number*.002,lng=121.42+number*.002,
                                  tags=[category,"验收"], private_note="仅甲可见的隔离验收备注",
                                  expected_stay_min=30))
            await db.commit()

        async def blocked_request(*args, **kwargs):
            amap.last_error = "隔离验收：未调用真实高德，路线为估算"
            return None
        async def geocode(address):
            return {"lat":31.22,"lng":121.42}
        async def search(keyword, city, limit=10):
            if "寿司郎" in keyword:
                return [{"name":"寿司郎验收商场店","lat":31.226,"lng":121.426,"category":"餐饮",
                         "poi_id":"fixture-brand-a","city":city,"address":"隔离验收购物中心"},
                        {"name":"寿司郎验收公园店","lat":31.228,"lng":121.428,"category":"餐饮",
                         "poi_id":"fixture-brand-b","city":city,"address":"隔离验收公园路"}][:limit]
            return [{"name":keyword,"lat":31.22,"lng":121.42,"category":"公园",
                     "poi_id":"fixture-anchor","city":city,"address":"隔离验收示例地址"}]
        async def nearby(location, **kwargs):
            if kwargs.get("types") == "050000":
                return [{"name":"验收餐厅","lat":31.225,"lng":121.425,"category":"餐饮",
                         "poi_id":"fixture-food","city":"上海市","address":"隔离验收"}]
            return [{"name":"验收湖畔公园","lat":31.22,"lng":121.42,"category":"公园",
                     "poi_id":"fixture-park","city":"上海市","address":"隔离验收"},
                    {"name":"验收美术馆","lat":31.222,"lng":121.422,"category":"博物馆",
                     "poi_id":"fixture-museum","city":"上海市","address":"隔离验收"}]
        amap.available = True
        amap._get = blocked_request
        amap.geocode = geocode
        amap.search_pois = search
        amap.nearby_pois = nearby

        if args.review_scenario:
            from sqlalchemy import select
            from app.services.planning_input import load_planning_input
            from app.services.itinerary import plan_itinerary
            async with sessions() as db:
                current_trip = await db.get(Trip, trip.id)
                places = [TripDestination(trip_id=trip.id, submitted_by=people[0].id,
                           name=name, lat=31.22+i*.002, lng=121.42+i*.002,
                           visit_status="must_visit", expected_stay_min=30)
                          for i, name in enumerate(("验收预约馆", "验收散步公园"))]
                db.add_all(places)
                await db.flush()
                for version in (1, 2):
                    current_trip.input_version = version
                    if version == 2:
                        current_trip.planned_end_at = current_trip.planned_start_at + timedelta(hours=1)
                        places[0].expected_stay_min = 90
                        places[0].visit_window_start = current_trip.planned_start_at
                        places[0].visit_window_end = current_trip.planned_end_at
                        members = (await db.execute(select(TripParticipant).where(TripParticipant.trip_id == trip.id))).scalars().all()
                        for member in members:
                            member.available_until = current_trip.planned_start_at + timedelta(minutes=90)
                    await db.flush()
                    inputs = await load_planning_input(db, current_trip)
                    result = await plan_itinerary(inputs.participants, inputs.destinations, inputs.start,
                                                  inputs.end, inputs.mode, inputs.objective)
                    db.add(ItineraryPlan(trip_id=trip.id, input_version=version, objective=inputs.objective,
                        **{key: result[key] for key in ("first_destination_id", "participant_arrivals", "ordered_stops",
                           "route_legs", "warnings", "total_travel_min", "total_duration_min")}))
                    await db.flush()
                await db.commit()

        @app.middleware("http")
        async def fixture_label(request, call_next):
            response = await call_next(request)
            response.headers["X-Acceptance-Fixture"] = "isolated"
            # The real application HTML is served unchanged; the browser tab
            # and random localhost port identify the isolated environment.
            return response

        sock = socket.socket()
        sock.bind(("127.0.0.2", 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="critical", access_log=False))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            if serving.done():
                await serving
                raise RuntimeError("Server failed before readiness")
            await asyncio.sleep(.05)
        args.state_file.write_text(json.dumps({"stage":"ready", "host":"127.0.0.2", "port":port, "schema":schema,
                                               "password":password, "users":["acceptance_1","acceptance_2","acceptance_3"],
                                               "trip_id":trip.id}, ensure_ascii=False), encoding="utf-8")
        deadline = asyncio.get_running_loop().time() + args.ttl
        while not args.stop_file.exists() and asyncio.get_running_loop().time() < deadline:
            if serving.done():
                await serving
                break
            await asyncio.sleep(.2)
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=20)
    finally:
        if sock:
            sock.close()
        await amap.aclose()
        await llm_place_parser.aclose()
        if engine:
            await engine.dispose()
        if created:
            # Only the exact schema generated by this invocation can be removed.
            assert schema.startswith("agent_test_") and len(schema) == 43
            async with admin.begin() as connection:
                await connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        await admin.dispose()
        args.state_file.write_text(json.dumps({"stage":"cleaned","schema":schema}), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--ttl", type=int, default=2700)
    parser.add_argument("--review-scenario", action="store_true", help="Seed two snapshots and time conflicts for route review")
    options = parser.parse_args()
    asyncio.run(run(options))
