"""Add destination fixed visit window.

Revision ID: 0008_destination_visit_window
Revises: 0007_place_parse_feedback
"""
from alembic import op
import sqlalchemy as sa


revision = "0008_destination_visit_window"
down_revision = "0007_place_parse_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("trip_destinations")}
    if "visit_window_start" not in columns:
        op.add_column("trip_destinations", sa.Column("visit_window_start", sa.TIMESTAMP(timezone=True)))
    if "visit_window_end" not in columns:
        op.add_column("trip_destinations", sa.Column("visit_window_end", sa.TIMESTAMP(timezone=True)))
    constraints = {item["name"] for item in sa.inspect(op.get_bind()).get_check_constraints("trip_destinations")}
    if "ck_trip_destination_visit_window_pair" not in constraints:
        op.create_check_constraint(
            "ck_trip_destination_visit_window_pair", "trip_destinations",
            "(visit_window_start IS NULL AND visit_window_end IS NULL) OR "
            "(visit_window_start IS NOT NULL AND visit_window_end IS NOT NULL AND visit_window_end > visit_window_start)",
        )


def downgrade() -> None:
    op.drop_constraint("ck_trip_destination_visit_window_pair", "trip_destinations", type_="check")
    op.drop_column("trip_destinations", "visit_window_end")
    op.drop_column("trip_destinations", "visit_window_start")
