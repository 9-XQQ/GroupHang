"""Phase 1 baseline，兼容已经由 create_all 建立的数据库。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_phase1"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "users" not in existing:
        op.create_table(
            "users",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("phone", sa.String(20), nullable=True, unique=True),
            sa.Column("wechat_openid", sa.String(64), nullable=True, unique=True),
            sa.Column("name", sa.String(50), nullable=False),
            sa.Column("home_location", postgresql.JSONB(), nullable=True),
            sa.Column("preferences", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        )
    if "trips" not in existing:
        op.create_table(
            "trips",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("title", sa.String(100), nullable=False),
            sa.Column("creator_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("invite_code", sa.String(8), nullable=False, unique=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="active"),
            sa.Column("default_mode", sa.String(20), nullable=False, server_default="transit"),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        )
    if "trip_participants" not in existing:
        op.create_table(
            "trip_participants",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("trip_id", sa.BigInteger(), sa.ForeignKey("trips.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("role", sa.String(10), nullable=False, server_default="member"),
            sa.Column("start_location", postgresql.JSONB(), nullable=True),
            sa.Column("transport_mode", sa.String(20), nullable=False, server_default="transit"),
            sa.Column("vote_status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("joined_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("trip_id", "user_id", name="uq_trip_participant"),
        )
    if "meeting_point_results" not in existing:
        op.create_table(
            "meeting_point_results",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("trip_id", sa.BigInteger(), sa.ForeignKey("trips.id", ondelete="CASCADE"), nullable=False),
            sa.Column("candidates", postgresql.JSONB(), nullable=False),
            sa.Column("objective", sa.String(20), nullable=True),
            sa.Column("computed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        )
    if "trip_votes" not in existing:
        op.create_table(
            "trip_votes",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("trip_id", sa.BigInteger(), sa.ForeignKey("trips.id", ondelete="CASCADE"), nullable=False),
            sa.Column("candidate_type", sa.String(20), nullable=False, server_default="meeting_point"),
            sa.Column("candidate_id", sa.String(64), nullable=False),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("vote_value", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("trip_id", "candidate_type", "candidate_id", "user_id", name="uq_trip_vote"),
        )


def downgrade() -> None:
    # baseline 可能接管已有数据库，自动删除旧表并不安全。
    pass
