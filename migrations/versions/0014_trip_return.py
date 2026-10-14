"""Persist a shared route return target; old trips keep no return leg."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0014_trip_return"
down_revision = "0013_saved_places"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("trips", sa.Column("return_location", postgresql.JSONB(), nullable=True))


def downgrade():
    op.drop_column("trips", "return_location")
