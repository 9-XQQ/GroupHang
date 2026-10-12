"""Persist Agent proposals and structured conversation state.

Revision ID: 0009_agent_state
Revises: 0008_destination_visit_window
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0009_agent_state"
down_revision = "0008_destination_visit_window"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "agent_proposals" not in tables:
        op.create_table(
            "agent_proposals",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("trip_id", sa.BigInteger(), sa.ForeignKey("trips.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("input_version", sa.Integer(), nullable=False),
            sa.Column("actions", postgresql.JSONB(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_agent_proposals_trip_id", "agent_proposals", ["trip_id"])
        op.create_index("ix_agent_proposals_user_id", "agent_proposals", ["user_id"])
        op.create_index("ix_agent_proposals_status", "agent_proposals", ["status"])
        op.create_index("ix_agent_proposals_expires_at", "agent_proposals", ["expires_at"])
    if "agent_conversation_states" not in tables:
        op.create_table(
            "agent_conversation_states",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("trip_id", sa.BigInteger(), sa.ForeignKey("trips.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("last_actions", postgresql.JSONB(), nullable=False, server_default="[]"),
            sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("trip_id", "user_id", name="uq_agent_conversation_state_user"),
        )
        op.create_index("ix_agent_conversation_states_trip_id", "agent_conversation_states", ["trip_id"])
        op.create_index("ix_agent_conversation_states_user_id", "agent_conversation_states", ["user_id"])
        op.create_index("ix_agent_conversation_states_expires_at", "agent_conversation_states", ["expires_at"])


def downgrade() -> None:
    op.drop_table("agent_conversation_states")
    op.drop_table("agent_proposals")
