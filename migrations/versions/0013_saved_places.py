"""Saved places, private memories, member comments and import replay."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0013_saved_places"
down_revision = "0012_password_auth"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("trip_participants", sa.Column("share_preferences", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table("saved_places",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("place_key", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("city", sa.String(30), nullable=False, server_default=""),
        sa.Column("address", sa.String(300)),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lng", sa.Float(), nullable=False),
        sa.Column("category", sa.String(80)),
        sa.Column("poi_id", sa.String(100)),
        sa.Column("tags", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("private_note", sa.String(2000), nullable=False, server_default=""),
        sa.Column("expected_stay_min", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "place_key", name="uq_saved_place_owner_key"))
    op.create_index("ix_saved_places_user_id", "saved_places", ["user_id"])
    op.create_table("destination_comments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("destination_id", sa.BigInteger(), sa.ForeignKey("trip_destinations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content", sa.String(1000), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()))
    for field in ("destination_id", "user_id"):
        op.create_index("ix_destination_comments_" + field, "destination_comments", [field])
    op.create_table("trip_place_imports",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trip_id", sa.BigInteger(), sa.ForeignKey("trips.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_key", sa.String(64), nullable=False),
        sa.Column("destination_id", sa.BigInteger(), sa.ForeignKey("trip_destinations.id", ondelete="CASCADE"), nullable=False),
        sa.UniqueConstraint("trip_id", "user_id", "source_key", name="uq_trip_place_import"))


def downgrade():
    op.drop_table("trip_place_imports")
    op.drop_table("destination_comments")
    op.drop_table("saved_places")
    op.drop_column("trip_participants", "share_preferences")
