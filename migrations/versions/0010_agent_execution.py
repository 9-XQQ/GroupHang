"""Agent execution leases, replay receipts and shared history invalidation.

Revision ID: 0010_agent_execution
Revises: 0009_agent_state
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010_agent_execution"
down_revision = "0009_agent_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_proposals", sa.Column("lease_token", sa.String(36), nullable=True))
    op.add_column("agent_proposals", sa.Column("lease_until", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("agent_proposals", sa.Column("result", postgresql.JSONB(), nullable=True))
    op.add_column("agent_conversation_states", sa.Column(
        "history_epoch", sa.String(36), nullable=False, server_default="initial",
    ))
    # Legacy executing rows may have partially committed business actions. Do
    # not retry them automatically. Scrub terminal/expired payloads on upgrade.
    op.execute("""
        UPDATE agent_proposals SET actions = '[]'::jsonb,
            status = CASE WHEN status = 'consumed' THEN status ELSE 'expired' END
        WHERE expires_at <= now() OR status IN ('consumed', 'expired', 'cancelled', 'executing')
    """)


def downgrade() -> None:
    op.drop_column("agent_conversation_states", "history_epoch")
    op.drop_column("agent_proposals", "result")
    op.drop_column("agent_proposals", "lease_until")
    op.drop_column("agent_proposals", "lease_token")
