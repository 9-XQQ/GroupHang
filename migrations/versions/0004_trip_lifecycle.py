"""Normalize trip lifecycle and add completion audit fields.

Revision ID: 0004_trip_lifecycle
Revises: 0003_availability
"""
from alembic import op
import sqlalchemy as sa


revision = "0004_trip_lifecycle"
down_revision = "0003_availability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("trips")}
    if "completed_at" not in columns:
        op.add_column("trips", sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True))
    if "completed_by" not in columns:
        op.add_column("trips", sa.Column("completed_by", sa.BigInteger(), nullable=True))
        op.create_foreign_key(
            "fk_trips_completed_by_users", "trips", "users", ["completed_by"], ["id"], ondelete="SET NULL"
        )
    op.execute("UPDATE trips SET status = 'confirmed' WHERE status = 'finished'")


def downgrade() -> None:
    op.execute("UPDATE trips SET status = 'finished' WHERE status = 'confirmed'")
    op.execute("UPDATE trips SET status = 'finished' WHERE status = 'completed'")
    op.drop_constraint("fk_trips_completed_by_users", "trips", type_="foreignkey")
    op.drop_column("trips", "completed_by")
    op.drop_column("trips", "completed_at")
