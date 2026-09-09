"""Add per-participant availability windows.

Revision ID: 0003_availability
Revises: 0002_phase2a
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_availability"
down_revision = "0002_phase2a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("trip_participants")}
    if "available_from" not in columns:
        op.add_column("trip_participants", sa.Column("available_from", sa.TIMESTAMP(timezone=True), nullable=True))
    if "available_until" not in columns:
        op.add_column("trip_participants", sa.Column("available_until", sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("trip_participants", "available_until")
    op.drop_column("trip_participants", "available_from")
