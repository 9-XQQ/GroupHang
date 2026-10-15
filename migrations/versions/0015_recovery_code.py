"""Single-use offline account recovery codes; existing users remain unchanged."""
from alembic import op
import sqlalchemy as sa
revision = "0015_recovery_code"
down_revision = "0014_trip_return"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("account_recovery_codes",
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False))


def downgrade():
    op.drop_table("account_recovery_codes")
