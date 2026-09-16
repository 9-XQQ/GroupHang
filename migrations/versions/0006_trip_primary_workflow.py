"""Add the creator-controlled primary workflow for each trip.

Revision ID: 0006_trip_primary_workflow
Revises: 0005_destination_feedback
"""
from alembic import op
import sqlalchemy as sa


revision = "0006_trip_primary_workflow"
down_revision = "0005_destination_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("trips")}
    if "primary_workflow" not in columns:
        op.add_column(
            "trips",
            sa.Column("primary_workflow", sa.String(20), nullable=False, server_default="itinerary"),
        )


def downgrade() -> None:
    op.drop_column("trips", "primary_workflow")
