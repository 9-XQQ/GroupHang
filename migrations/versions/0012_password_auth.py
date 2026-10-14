"""Username/password credentials and shared login request budgets."""
from alembic import op
import sqlalchemy as sa

revision = "0012_password_auth"
down_revision = "0011_phone_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("password_credentials",
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("username", sa.String(32), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("auth_rate_limits",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.TIMESTAMP(timezone=True), nullable=False))
    op.create_index("ix_auth_rate_limits_window_started_at", "auth_rate_limits", ["window_started_at"])


def downgrade() -> None:
    op.drop_table("auth_rate_limits")
    op.drop_table("password_credentials")
