"""Frozen synthetic task evaluation: real isolated PostgreSQL, offline tool fixtures."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from unittest.mock import AsyncMock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db import Base
from app.models import AgentProposal, ItineraryPlan, SavedPlace, Trip, TripDestination, TripParticipant, User
from app.routers.agent import chat
from app.schemas import AgentChatRequest, DiscoveryRequest
from app.services import place_discovery as discovery
from scripts.evaluate_agent import percentile

PRIVATE_MARKER = "SYNTHETIC_PRIVATE_NOTE_NOT_FOR_REPORT"


def load_tasks(path):
    raw = path.read_bytes()
    dataset = json.loads(raw)
    episodes = dataset.get("episodes")
    if not isinstance(episodes, list) or not episodes:
        raise ValueError("Nonempty episodes required")
    seen = set()
    for case in episodes:
        identifier = case.get("id", "")
        if not re.fullmatch(r"[a-z0-9_]{1,80}", identifier) or identifier in seen:
            raise ValueError("Invalid or duplicate episode ID")
        seen.add(identifier)
        if case.get("scenario") not in {"meal", "brand", "afternoon"} or not isinstance(case.get("turns"), list) or len(case["turns"]) < 2:
            raise ValueError("Invalid scenario/turns")
        if set(case.get("initial", {})) - {"missing_origin", "preferences", "saved_brand", "missing_time"}:
            raise ValueError("Unknown initial state")
        request = {"scenario": case["scenario"], **case.get("request", {})}
        for turn in case["turns"]:
            if set(turn.get("tools", {})) - {"search_outage", "route_outage"} or set(turn.get("state", {})) - {"restore_origin"}:
                raise ValueError("Unknown tool/state fixture")
            request.update(turn.get("request", {}))
            if request["scenario"] != case["scenario"]:
                raise ValueError("Scenario cannot change within an episode")
            DiscoveryRequest.model_validate(request)
            allowed = {"needs_contains", "min_recommendations", "max_recommendations", "top", "names",
                       "brand_only", "reason_contains", "reason_absent", "warning_contains",
                       "anchor_choices", "finish_within", "estimated", "return_leg"}
            expected = turn.get("expected")
            if not isinstance(expected, dict) or not expected or set(expected) - allowed:
                raise ValueError("Missing or unknown expected checks")
    return dataset, hashlib.sha256(raw).hexdigest()


@asynccontextmanager
async def isolated_sessions():
    # Explicit opt-in; one random schema only, never create/drop an existing DB.
    if os.getenv("AGENT_DB_TESTS") != "1":
        raise ValueError("Set AGENT_DB_TESTS=1 to allow an isolated temporary schema")
    url = os.getenv("TEST_DATABASE_URL", settings.database_url)
    schema = "discovery_eval_" + uuid4().hex
    admin = create_async_engine(url, echo=False)
    engine = None
    created = False
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        created = True
        engine = create_async_engine(url, echo=False, connect_args={"server_settings": {"search_path": schema}})
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        if engine is not None:
            await engine.dispose()
        if created:
            assert re.fullmatch(r"discovery_eval_[0-9a-f]{32}", schema)
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


def point(name, lng, category="餐饮", **extras):
    return {"name": name, "lat": 31.2, "lng": lng, "category": category,
            "city": "合成市", "address": "合成地址", "poi_id": name, **extras}


class SyntheticTools:
    def __init__(self):
        self.options = {}
        self.calls = {"search": 0, "nearby": 0, "route": 0, "model": 0}

    async def model(self, *args, **kwargs):
        self.calls["model"] += 1
        return None, "offline_task_eval"

    async def search(self, keywords, city, limit=10):
        self.calls["search"] += 1
        if self.options.get("search_outage"):
            return []  # Adapter contract for unavailable/empty POI search.
        if keywords == "同名公园":
            return [point("同名公园", 121.42, "公园"), point("同名公园", 121.43, "公园")]
        return [point("示例寿司中点店", 121.42), point("示例寿司公园店", 121.421),
                point("其他品牌", 121.42)]

    async def nearby(self, location, **kwargs):
        self.calls["nearby"] += 1
        if kwargs.get("types") == "110000|060100":
            return [point("合成公园", 121.421, "公园")] if abs(location[1] - 121.421) < 0.0001 else []
        if kwargs.get("types") == "050000":
            return [point("中点餐厅", 121.42), point("偏东餐厅", 121.439)]
        return [point("合成展馆", 121.42, "博物馆"), point("合成花园", 121.425, "公园")]

    async def route(self, origin, dest, mode):
        self.calls["route"] += 1
        if self.options.get("route_outage"):
            return None  # Real itinerary._route performs the estimation fallback.
        return {"duration_min": 5 + abs(dest[1] - origin[1]) * 1000,
                "distance_km": 1, "polyline": [[origin[1], origin[0]], [dest[1], dest[0]]], "steps": []}


async def seed(db, case):
    initial = case.get("initial", {})
    prefs = {"personalization_enabled": True, "explicit": {"preferred_categories": ["公园"]}}
    owner = User(name="合成甲", preferences=prefs if initial.get("preferences") else {})
    other = User(name="合成乙")
    db.add_all([owner, other])
    await db.flush()
    trip = Trip(title="合成评测行程", creator_id=owner.id, invite_code=uuid4().hex[:8],
                planned_start_at=None if initial.get("missing_time") else datetime(2026, 11, 8, 5, tzinfo=timezone.utc),
                planned_end_at=None if initial.get("missing_time") else datetime(2026, 11, 8, 9, tzinfo=timezone.utc))
    db.add(trip)
    await db.flush()
    member = TripParticipant(trip_id=trip.id, user_id=owner.id, role="creator",
                             start_location={"lat": 31.2, "lng": 121.4})
    second = TripParticipant(trip_id=trip.id, user_id=other.id, role="member",
                             start_location=None if initial.get("missing_origin") else {"lat": 31.2, "lng": 121.44})
    db.add_all([member, second])
    if case["id"] == "meal_saved_tags":
        for name, tag, lng in [("周末餐厅", "周末", 121.42), ("雨天餐厅", "雨天", 121.421)]:
            db.add(SavedPlace(user_id=owner.id, place_key=hashlib.sha256(name.encode()).hexdigest(),
                             name=name, city="合成市", lat=31.2, lng=lng, category="餐饮",
                             tags=[tag], private_note=PRIVATE_MARKER, expected_stay_min=60))
    if initial.get("saved_brand"):
        db.add(SavedPlace(user_id=owner.id, place_key="synthetic_brand", name="示例寿司收藏店", city="合成市", lat=31.2, lng=121.419,
                          category="餐饮", tags=[], private_note=PRIVATE_MARKER, expected_stay_min=60))
    await db.commit()
    return owner, trip, second


def score_turn(result, expected):
    items = result.get("recommendations", [])
    names = [item["place"]["name"] for item in items]
    reasons = " ".join(reason for item in items for reason in item.get("reasons", []))
    checks = {"no_private_note": PRIVATE_MARKER not in json.dumps(result, default=str),
              "preview_only": result.get("saved") is False and not result.get("actions") and not result.get("proposal_id"),
              "recommendation_bound": len(items) <= (5 if result.get("scenario") == "afternoon" else 3),
              "needs_excludes_recommendations": not (result.get("needs") and items)}
    for key, wanted in expected.items():
        if key == "needs_contains":
            checks[key] = wanted in " ".join(result.get("needs", []))
        elif key == "min_recommendations":
            checks[key] = len(items) >= wanted
        elif key == "max_recommendations":
            checks[key] = len(items) <= wanted
        elif key == "top":
            checks[key] = bool(names) and names[0] == wanted
        elif key == "names":
            checks[key] = sorted(names) == sorted(wanted)
        elif key == "brand_only":
            checks[key] = bool(names) and all(wanted in name for name in names)
        elif key == "reason_contains":
            checks[key] = wanted in reasons
        elif key == "reason_absent":
            checks[key] = wanted not in reasons
        elif key == "warning_contains":
            checks[key] = wanted in " ".join(result.get("warnings", []))
        elif key == "anchor_choices":
            checks[key] = len(result.get("anchor_choices", [])) == wanted
        elif key == "finish_within":
            checks[key] = result.get("finish_at") is not None and result.get("end_at") is not None and result["finish_at"] <= result["end_at"]
        elif key == "return_leg":
            checks[key] = bool(result.get("return_leg")) == wanted
        elif key == "estimated":
            checks[key] = bool(items) and all(bool(item.get("estimated")) == wanted for item in items)
        else:
            raise ValueError("Unknown check")
    return checks


async def evaluate_episode(sessions, case):
    tools = SyntheticTools()
    turns = []
    async with sessions() as db:
        owner, trip, second = await seed(db, case)
        request = {"scenario": case["scenario"], **case.get("request", {})}
        with (patch.object(discovery.llm_place_parser, "complete_json", tools.model),
              patch.object(discovery.amap, "search_pois", tools.search),
              patch.object(discovery.amap, "nearby_pois", tools.nearby),
              patch.object(discovery.amap, "route_detail", tools.route),
              patch.object(discovery.amap, "available", True),
              patch("httpx.AsyncClient.send", AsyncMock(side_effect=AssertionError("Network forbidden")))):
            for number, turn in enumerate(case["turns"], 1):
                request.update(turn.get("request", {}))
                tools.options.update(turn.get("tools", {}))
                if turn.get("state", {}).get("restore_origin"):
                    second.start_location = {"lat": 31.2, "lng": 121.44}
                    trip.input_version += 1
                    await db.commit()
                before_calls = dict(tools.calls)
                expected_version = trip.input_version
                started = time.perf_counter()
                error_type = None
                try:
                    body = AgentChatRequest(message="合成场景评测", discovery=DiscoveryRequest.model_validate(request))
                    answer = await chat(trip.id, body, owner, db)
                    checks = score_turn(answer["discovery"], turn["expected"])
                    # Count actual persistent side effects, rather than trusting saved=False.
                    for model in (TripDestination, AgentProposal, ItineraryPlan):
                        count = await db.scalar(select(func.count()).select_from(model).where(model.trip_id == trip.id))
                        checks["no_write_" + model.__tablename__] = count == 0
                    checks["version_unchanged"] = (await db.get(Trip, trip.id, populate_existing=True)).input_version == expected_version
                except Exception as exc:
                    error_type = type(exc).__name__
                    checks = {"no_exception": False}
                    await db.rollback()
                turns.append({"turn": number, "passed": all(checks.values()), "checks": checks,
                              "error_type": error_type, "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                              "fixture_calls": {key: tools.calls[key] - before_calls[key] for key in tools.calls}})
    return {"id": case["id"], "scenario": case["scenario"], "passed": all(t["passed"] for t in turns), "turns": turns}


async def run(args):
    dataset, fingerprint = load_tasks(args.cases)
    results = []
    async with isolated_sessions() as sessions:
        for case in dataset["episodes"]:
            results.append(await evaluate_episode(sessions, case))
    turns = [turn for result in results for turn in result["turns"]]
    metrics = {"episodes": len(results), "passed_episodes": sum(r["passed"] for r in results),
               "turns": len(turns), "passed_turns": sum(t["passed"] for t in turns),
               "exceptions": sum(t["error_type"] is not None for t in turns),
               "p50_latency_ms": percentile([t["latency_ms"] for t in turns], .5),
               "p95_latency_ms": percentile([t["latency_ms"] for t in turns], .95)}
    metrics["scenarios"] = {scenario: {
        "episodes": sum(r["scenario"] == scenario for r in results),
        "passed": sum(r["scenario"] == scenario and r["passed"] for r in results),
    } for scenario in ("meal", "brand", "afternoon")}
    sources = ["scripts/evaluate_discovery.py", "app/services/place_discovery.py", "app/services/itinerary.py", "app/routers/discovery.py", "app/routers/agent.py"]
    report = {"report_version": 1, "split": dataset.get("split"), "dataset_sha256": fingerprint,
              "generated_at": datetime.now(timezone.utc).isoformat(), "network_disabled": True,
              "real_model_calls": 0, "real_map_calls": 0, "database": "isolated_random_schema",
              "source_sha256": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in sources},
              "metrics": metrics, "episodes": results}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows = ["# Synthetic discovery task evaluation", "", "Real isolated PostgreSQL; fixture tools; no real model/map calls.",
            "Structured conditions persist between turns in the harness; this does not test natural-language memory.",
            "", f"Episodes: {metrics['passed_episodes']}/{len(results)}; turns: {metrics['passed_turns']}/{len(turns)}.",
            "", "| Episode | Scenario | Pass | Failed checks |", "|---|---|---|---|"]
    for result in results:
        failures = [f"{t['turn']}:{key}" for t in result["turns"] for key, passed in t["checks"].items() if not passed]
        rows.append(f"| {result['id']} | {result['scenario']} | {result['passed']} | {', '.join(failures)} |")
    (args.output_dir / "report.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(json.dumps(metrics))
    return 0 if all(r["passed"] for r in results) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/discovery_tasks.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "docs_private/discovery_evals/latest")
    try:
        return asyncio.run(run(parser.parse_args()))
    except Exception as exc:
        print("Task evaluation failed: " + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
