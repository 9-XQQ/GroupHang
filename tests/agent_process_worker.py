"""Subprocess fixture for isolated Agent crash/retention tests. No external APIs."""
import argparse
import asyncio
import json
import re
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.models import AgentProposal, User
from app.routers import agent
from app.schemas import AgentProposalExecuteRequest
from app.services import agent_state


def signal(path: Path, stage: str, **values) -> None:
    path.write_text(json.dumps({"stage": stage, **values}), encoding="utf-8")


async def main(args) -> None:
    if not re.fullmatch(r"agent_test_[0-9a-f]{32}", args.schema):
        raise ValueError("fixture requires a disposable test schema")
    engine = create_async_engine(settings.database_url, echo=False, connect_args={
        "server_settings": {"search_path": args.schema},
    })
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    # Accelerate only the fixture's real database lease. Production is 2 minutes.
    agent_state.LEASE_TTL = timedelta(seconds=2)
    try:
        if args.mode == "retention":
            from scripts.cleanup_agent_state import run_worker
            signal(args.stage_file, "retention_started")
            await run_worker(args.status_file, True, sessions=sessions, interval=0.1)
            return
        if args.mode == "http_server":
            import socket
            import uvicorn
            from app.db import get_db
            from app.main import app
            from app import security
            from app.services.amap import amap
            from app.services.llm_parser import llm_place_parser

            async def isolated_db():
                async with sessions() as db:
                    yield db
            async def fixture_geocode(address):
                return {"lat": 39.8822, "lng": 116.4066}
            app.dependency_overrides[get_db] = isolated_db
            settings.sms_provider = "mock"
            settings.app_env = "development"
            settings.auth_secret = "isolated-test-secret-not-for-production"
            # Config is also overridden in the child environment. These settings
            # make accidental external calls impossible for this fixture.
            settings.amap_api_key = ""
            settings.llm_api_key = ""
            amap.geocode = fixture_geocode
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen(128)
            port = sock.getsockname()[1]
            config = uvicorn.Config(app, log_level="critical", access_log=False, lifespan="off")
            server = uvicorn.Server(config)
            serving = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                while not server.started:
                    if serving.done():
                        await serving
                        raise RuntimeError("fixture server stopped before startup")
                    await asyncio.sleep(0.05)
                signal(args.stage_file, "http_server", port=port)
                await serving
            finally:
                sock.close()
                await amap.aclose()
                await llm_place_parser.aclose()
            return
        async with sessions() as db:
            user = await db.get(User, args.user_id)
            if user is None:
                raise ValueError("fixture user missing")

            async def route(*route_args, **kwargs):
                if args.mode == "preparing":
                    signal(args.stage_file, "preparing")
                    await asyncio.Event().wait()
                return {
                    "first_destination_id": args.destination_id,
                    "participant_arrivals": [], "ordered_stops": [], "route_legs": [],
                    "warnings": [], "total_travel_min": 10, "total_duration_min": 80,
                    "group_start_at": "2026-11-01T02:00:00Z", "total_distance_km": 2.5,
                }
            agent.plan_itinerary = route
            if args.mode == "before_commit":
                def pause_before_commit(session):
                    if any(isinstance(value, AgentProposal) and value.status == "consumed"
                           for value in session.dirty):
                        signal(args.stage_file, "before_commit")
                        # Deliberately hold the uncommitted transaction until the
                        # parent terminates this exact child process.
                        time.sleep(60)
                        raise RuntimeError("parent did not terminate fixture")
                event.listen(db.sync_session, "before_commit", pause_before_commit)
            if args.mode == "after_commit":
                original = agent._commit_execution
                async def pause_after_commit(*values, **kwargs):
                    result = await original(*values, **kwargs)
                    signal(args.stage_file, "after_commit", plan_id=result["itinerary"]["plan_id"])
                    await asyncio.Event().wait()
                agent._commit_execution = pause_after_commit
            result = await agent.execute_proposal(
                args.trip_id, AgentProposalExecuteRequest(proposal_id=args.proposal_id), user, db,
            )
            signal(args.stage_file, "completed", result=result)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["preparing", "before_commit", "after_commit", "execute", "retention", "http_server"], required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--stage-file", type=Path, required=True)
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--proposal-id")
    parser.add_argument("--trip-id", type=int)
    parser.add_argument("--user-id", type=int)
    parser.add_argument("--destination-id", type=int)
    args = parser.parse_args()
    try:
        asyncio.run(main(args))
    except Exception as exc:
        signal(args.stage_file, "failed", error_type=type(exc).__name__)
        raise SystemExit(1)
