"""Preserve native counter evidence separately from additive usage deltas."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0021_usage_observations"
down_revision = "0020_milestone_contracts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("usage_record", sa.Column("accounting", pg.JSONB(), nullable=True))
    op.create_index("ix_usage_counter_identity", "usage_record", [
        sa.text("(accounting ->> 'identity')"), sa.text("(accounting ->> 'epoch')")])


def downgrade():
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
