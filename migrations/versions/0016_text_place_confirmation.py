"""Keep text confirmation fingerprints and deletion tombstones; no new tables."""
from alembic import op
import sqlalchemy as sa

revision = "0016_text_place_confirmation"
down_revision = "0015_recovery_code"
branch_labels = None
depends_on = None

FK_NAME = "trip_place_imports_destination_id_fkey"


def upgrade():
    op.add_column("trip_place_imports", sa.Column("request_hash", sa.String(64), nullable=True))
    op.drop_constraint(FK_NAME, "trip_place_imports", type_="foreignkey")
    op.alter_column("trip_place_imports", "destination_id", existing_type=sa.BigInteger(), nullable=True)
    op.create_foreign_key(FK_NAME, "trip_place_imports", "trip_destinations",
                          ["destination_id"], ["id"], ondelete="SET NULL")


def downgrade():
    # Do not silently discard durable receipts or destroy deletion tombstones.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM trip_place_imports
                   WHERE request_hash IS NOT NULL OR destination_id IS NULL) THEN
            RAISE EXCEPTION 'Text confirmation receipts exist; review/archive them before downgrade';
        END IF;
    END $$""")
    op.drop_constraint(FK_NAME, "trip_place_imports", type_="foreignkey")
    op.alter_column("trip_place_imports", "destination_id", existing_type=sa.BigInteger(), nullable=False)
    op.create_foreign_key(FK_NAME, "trip_place_imports", "trip_destinations",
                          ["destination_id"], ["id"], ondelete="CASCADE")
    op.drop_column("trip_place_imports", "request_hash")
