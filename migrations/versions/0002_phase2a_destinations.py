"""Phase 2A planning fields and collaborative destinations."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_phase2a"
down_revision = "0001_phase1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    trip_columns = {column["name"] for column in inspector.get_columns("trips")}
    additions = [
        ("group_transport_mode", sa.String(20), "'transit'"),
        ("planned_start_at", sa.TIMESTAMP(timezone=True), None),
        ("planned_end_at", sa.TIMESTAMP(timezone=True), None),
        ("optimization_objective", sa.String(20), "'balanced'"),
        ("input_version", sa.Integer(), "1"),
    ]
    for name, type_, default in additions:
        if name not in trip_columns:
            op.add_column(
                "trips",
                sa.Column(name, type_, nullable=default is None, server_default=sa.text(default) if default else None),
            )

    existing = set(sa.inspect(bind).get_table_names())
    if "trip_destinations" not in existing:
        op.create_table(
            "trip_destinations",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("trip_id", sa.BigInteger(), sa.ForeignKey("trips.id", ondelete="CASCADE"), nullable=False),
            sa.Column("submitted_by", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("address", sa.String(300), nullable=True),
            sa.Column("lat", sa.Float(), nullable=False),
            sa.Column("lng", sa.Float(), nullable=False),
            sa.Column("category", sa.String(80), nullable=True),
            sa.Column("visit_status", sa.String(20), nullable=False, server_default="candidate"),
            sa.Column("expected_stay_min", sa.Integer(), nullable=False, server_default="60"),
            sa.Column("opening_hours", postgresql.JSONB(), nullable=True),
            sa.Column("note", sa.String(500), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_trip_destinations_trip_id", "trip_destinations", ["trip_id"])

    existing = set(sa.inspect(bind).get_table_names())
    if "itinerary_plans" not in existing:
        op.create_table(
            "itinerary_plans",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("trip_id", sa.BigInteger(), sa.ForeignKey("trips.id", ondelete="CASCADE"), nullable=False),
            sa.Column("input_version", sa.Integer(), nullable=False),
            sa.Column("objective", sa.String(20), nullable=False),
            sa.Column("first_destination_id", sa.BigInteger(), sa.ForeignKey("trip_destinations.id", ondelete="SET NULL"), nullable=True),
            sa.Column("participant_arrivals", postgresql.JSONB(), nullable=False),
            sa.Column("ordered_stops", postgresql.JSONB(), nullable=False),
            sa.Column("route_legs", postgresql.JSONB(), nullable=False),
            sa.Column("warnings", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("total_travel_min", sa.Integer(), nullable=True),
            sa.Column("total_duration_min", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
            sa.Column("computed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_itinerary_plans_trip_id", "itinerary_plans", ["trip_id"])


def downgrade() -> None:
    op.drop_table("itinerary_plans")
    op.drop_table("trip_destinations")
    for name in ["input_version", "optimization_objective", "planned_end_at", "planned_start_at", "group_transport_mode"]:
        op.drop_column("trips", name)
