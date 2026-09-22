"""Add privacy-bounded place parse feedback.

Revision ID: 0007_place_parse_feedback
Revises: 0006_trip_primary_workflow
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0007_place_parse_feedback"
down_revision = "0006_trip_primary_workflow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "place_parse_feedback" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "place_parse_feedback",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trip_id", sa.BigInteger(), sa.ForeignKey("trips.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parse_session_id", sa.String(36), nullable=False),
        sa.Column("candidate_id", sa.String(36), nullable=False),
        sa.Column("parser", sa.String(80), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("proposed_place", postgresql.JSONB(), nullable=False),
        sa.Column("final_place", postgresql.JSONB(), nullable=True),
        sa.Column("consent_to_improve", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "trip_id", "user_id", "parse_session_id", "candidate_id",
            name="uq_place_parse_feedback_candidate",
        ),
        sa.CheckConstraint("action IN ('accepted', 'edited', 'rejected')", name="ck_place_parse_feedback_action"),
    )
    op.create_index("ix_place_parse_feedback_trip_id", "place_parse_feedback", ["trip_id"])
    op.create_index("ix_place_parse_feedback_user_id", "place_parse_feedback", ["user_id"])


def downgrade() -> None:
    op.drop_table("place_parse_feedback")
