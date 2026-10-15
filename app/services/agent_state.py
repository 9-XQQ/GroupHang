"""Database-backed Agent leases and bounded payload retention.

Helpers participate in the caller's transaction; only the sweeper owns its session.
"""
import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db import SessionLocal
from ..models import AgentConversationState, AgentProposal

logger = logging.getLogger(__name__)
PROPOSAL_TTL = timedelta(minutes=10)
LEASE_TTL = timedelta(minutes=2)
RECEIPT_TTL = timedelta(hours=24)


def now() -> datetime:
    return datetime.now(timezone.utc)


async def cleanup_agent_data(db: AsyncSession) -> None:
    from .auth_retention import cleanup_auth_data
    await cleanup_auth_data(db)
    clock = func.now()
    # Keep the epoch tombstone: deleting it could resurrect another worker's
    # pre-clear in-memory history or a chat response still in flight.
    await db.execute(update(AgentConversationState).where(
        AgentConversationState.expires_at <= clock,
        AgentConversationState.last_actions != [],
    ).values(last_actions=[]))
    await db.execute(update(AgentProposal).where(
        AgentProposal.expires_at <= clock,
        AgentProposal.status.in_(["pending", "executing"]),
    ).values(status="expired", actions=[], lease_token=None, lease_until=None, updated_at=clock))
    await db.execute(update(AgentProposal).where(
        AgentProposal.status.in_(["consumed", "expired", "cancelled"]),
        AgentProposal.actions != [],
    ).values(actions=[], lease_token=None, lease_until=None))
    await db.execute(delete(AgentProposal).where(
        AgentProposal.expires_at <= clock - RECEIPT_TTL,
    ))


async def sweep_agent_data() -> None:
    from .agent_assistant import agent_assistant
    from .agent_trace import _prune

    while True:
        agent_assistant.cleanup()
        _prune()
        try:
            async with SessionLocal() as db:
                await cleanup_agent_data(db)
                await db.commit()
        except Exception:
            # Never log SQL parameters or proposal payloads.
            logger.error("Agent retention sweep failed; retrying in five seconds")
        await asyncio.sleep(5)


async def history_state(db: AsyncSession, trip_id: int, user_id: int) -> AgentConversationState:
    await db.execute(insert(AgentConversationState).values(
        trip_id=trip_id, user_id=user_id, last_actions=[],
        history_epoch=str(uuid4()), expires_at=now() + timedelta(minutes=30),
    ).on_conflict_do_nothing(constraint="uq_agent_conversation_state_user"))
    return (await db.execute(select(AgentConversationState).where(
        AgentConversationState.trip_id == trip_id,
        AgentConversationState.user_id == user_id,
    ).execution_options(populate_existing=True))).scalar_one()


async def claim_proposal(db: AsyncSession, proposal_id: str, trip_id: int, user_id: int) -> AgentProposal | None:
    token = str(uuid4())
    result = await db.execute(update(AgentProposal).where(
        AgentProposal.id == proposal_id, AgentProposal.trip_id == trip_id,
        AgentProposal.user_id == user_id, AgentProposal.expires_at > func.clock_timestamp(),
        or_(AgentProposal.status == "pending", and_(
            AgentProposal.status == "executing",
            or_(AgentProposal.lease_until.is_(None), AgentProposal.lease_until <= func.clock_timestamp()),
        )),
    ).values(
        status="executing", lease_token=token,
        lease_until=func.clock_timestamp() + LEASE_TTL, updated_at=func.now(),
    ).returning(AgentProposal).execution_options(populate_existing=True))
    return result.scalar_one_or_none()


async def release_proposal(db: AsyncSession, proposal_id: str, token: str) -> None:
    # A failed/stale worker must never release a newer worker's lease.
    await db.execute(update(AgentProposal).where(
        AgentProposal.id == proposal_id, AgentProposal.status == "executing",
        AgentProposal.lease_token == token,
    ).values(status="pending", lease_token=None, lease_until=None, updated_at=func.now()))


@asynccontextmanager
async def maintain_lease(db: AsyncSession, proposal_id: str, token: str):
    """Renew slow computations without holding Trip locks or exceeding expiry."""
    sessions = async_sessionmaker(db.bind, expire_on_commit=False)

    async def heartbeat():
        while True:
            await asyncio.sleep(20)
            try:
                async with sessions() as session:
                    result = await session.execute(update(AgentProposal).where(
                        AgentProposal.id == proposal_id,
                        AgentProposal.status == "executing", AgentProposal.lease_token == token,
                        AgentProposal.expires_at > func.clock_timestamp(),
                        AgentProposal.lease_until > func.clock_timestamp(),
                    ).values(lease_until=func.clock_timestamp() + LEASE_TTL))
                    await session.commit()
                    if result.rowcount != 1:
                        return
            except Exception:
                logger.error("Agent lease renewal failed; final commit will revalidate ownership")
                return

    task = asyncio.create_task(heartbeat())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
