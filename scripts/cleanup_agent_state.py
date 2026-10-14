"""Agent retention worker, independent of the Web application's lifecycle.

Default: one sweep. --watch: supervise retention every five seconds.
--status: inspect the last successful cleanup without opening the database.
Only timestamps/counters/error class names are written; no SQL or payloads.
"""
import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATUS = ROOT / "logs" / "agent_retention_status.json"
INTERVAL_SECONDS = 5
SWEEP_TIMEOUT_SECONDS = 10
STALE_SECONDS = 30


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_status(path: Path, status: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    temporary.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def inspect_status(path: Path) -> tuple[dict, int]:
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
        successful = datetime.fromisoformat(status["last_success_at"])
        age = (datetime.now(timezone.utc) - successful).total_seconds()
        healthy = 0 <= age <= STALE_SECONDS and status.get("consecutive_failures") == 0
        return {**status, "healthy": healthy, "success_age_seconds": round(age, 1)}, 0 if healthy else 1
    except (OSError, ValueError, KeyError, TypeError):
        return {"healthy": False, "reason": "missing_or_invalid_status"}, 1


async def run_worker(status_path: Path, watch: bool, sessions=None, interval: float = INTERVAL_SECONDS) -> int:
    # Lazy imports keep --status independent of database configuration and access.
    from app.db import SessionLocal
    from app.services.agent_state import cleanup_agent_data

    sessions = sessions or SessionLocal
    # SQL errors must not expose SQL/parameters, even with DEBUG=True in .env.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    bind = sessions.kw.get("bind")
    if bind is not None:
        bind.echo = False
    state = {"worker_pid": os.getpid(), "started_at": timestamp(),
             "last_success_at": None, "last_failure_at": None, "consecutive_failures": 0,
             "last_error_type": None, "sweeps_completed": 0}

    async def sweep():
        async with sessions() as db:
            await cleanup_agent_data(db)
            await db.commit()

    while True:
        try:
            await asyncio.wait_for(sweep(), timeout=SWEEP_TIMEOUT_SECONDS)
            state.update(last_success_at=timestamp(), consecutive_failures=0, last_error_type=None)
            state["sweeps_completed"] += 1
        except Exception as exc:
            state.update(last_failure_at=timestamp(), last_error_type=type(exc).__name__)
            state["consecutive_failures"] += 1
        state["last_attempt_at"] = timestamp()
        write_status(status_path, state)
        if not watch:
            return 0 if state["consecutive_failures"] == 0 else 1
        await asyncio.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--watch", action="store_true")
    mode.add_argument("--status", action="store_true")
    parser.add_argument("--status-file", type=Path, default=DEFAULT_STATUS)
    args = parser.parse_args()
    if args.status:
        result, code = inspect_status(args.status_file)
        print(json.dumps(result, ensure_ascii=False))
        return code

    async def execute():
        from app.db import engine
        try:
            return await run_worker(args.status_file, args.watch)
        finally:
            await engine.dispose()

    code = asyncio.run(execute())
    if not args.watch:
        print("Agent retention sweep completed" if code == 0 else "Agent retention sweep failed; inspect --status")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
