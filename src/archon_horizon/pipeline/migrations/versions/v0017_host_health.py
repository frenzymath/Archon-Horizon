"""Observe worker health independently of work admission."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0017_host_health"
down_revision = "0016_source_projection"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("host", sa.Column("health", postgresql.JSONB(), nullable=True))
    op.create_check_constraint(op.f("ck_host_health_object"), "host",
                               "health IS NULL OR jsonb_typeof(health) = 'object'")


def downgrade():
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
