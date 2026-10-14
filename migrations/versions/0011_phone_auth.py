"""Phone OTP challenges and database sessions.

Revision ID: 0011_phone_auth
Revises: 0010_agent_execution
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_phone_auth"
down_revision = "0010_agent_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    for column in ("user_id", "expires_at"):
        op.create_index("ix_auth_sessions_" + column, "auth_sessions", [column])
    op.create_table(
        "sms_challenges",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("phone_hash", sa.String(64), nullable=False),
        sa.Column("ip_hash", sa.String(64), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    for column in ("phone_hash", "ip_hash", "created_at", "expires_at"):
        op.create_index("ix_sms_challenges_" + column, "sms_challenges", [column])


def downgrade() -> None:
    op.drop_table("sms_challenges")
    op.drop_table("auth_sessions")
