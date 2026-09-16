"""Add per-user destination feedback for completed trips.

Revision ID: 0005_destination_feedback
Revises: 0004_trip_lifecycle
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0005_destination_feedback"
down_revision = "0004_trip_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "destination_feedback" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "destination_feedback",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trip_id", sa.BigInteger(), sa.ForeignKey("trips.id", ondelete="CASCADE"), nullable=False),
        sa.Column("destination_id", sa.BigInteger(), sa.ForeignKey("trip_destinations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("visited", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("rating", sa.SmallInteger(), nullable=True),
        sa.Column("actual_stay_min", sa.Integer(), nullable=True),
        sa.Column("tags", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("comment", sa.String(500), nullable=True),
        sa.Column("would_revisit", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("trip_id", "destination_id", "user_id", name="uq_destination_feedback_user"),
        sa.CheckConstraint("rating IS NULL OR (rating >= 1 AND rating <= 5)", name="ck_destination_feedback_rating"),
        sa.CheckConstraint(
            "actual_stay_min IS NULL OR (actual_stay_min >= 0 AND actual_stay_min <= 1440)",
            name="ck_destination_feedback_stay",
        ),
    )
    op.create_index("ix_destination_feedback_trip_id", "destination_feedback", ["trip_id"])
    op.create_index("ix_destination_feedback_destination_id", "destination_feedback", ["destination_id"])


def downgrade() -> None:
    op.drop_table("destination_feedback")
