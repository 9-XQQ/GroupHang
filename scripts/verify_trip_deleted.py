"""Verify that deleting a Trip cascaded to all Trip-scoped Phase 4 data."""
import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import engine  # noqa: E402


TABLES = (
    "trips",
    "trip_participants",
    "trip_destinations",
    "destination_feedback",
    "place_parse_feedback",
    "itinerary_plans",
)


async def verify(trip_id: int) -> None:
    counts = {}
    async with engine.connect() as connection:
        for table in TABLES:
            column = "id" if table == "trips" else "trip_id"
            result = await connection.execute(
                text(f"SELECT count(*) FROM {table} WHERE {column} = :trip_id"),
                {"trip_id": trip_id},
            )
            counts[table] = result.scalar_one()
    await engine.dispose()
    print(counts)
    remaining = {table: count for table, count in counts.items() if count}
    if remaining:
        raise SystemExit(f"Trip {trip_id} 仍有级联数据残留：{remaining}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("trip_id", type=int)
    arguments = parser.parse_args()
    asyncio.run(verify(arguments.trip_id))
