"""Retain agent-authored native child descriptions independently of model options."""

from alembic import op
import sqlalchemy as sa

revision = "0018_subagent_description"
down_revision = "0017_host_health"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("provider_thread", sa.Column("label", sa.String(256), nullable=True))
    op.add_column("provider_thread", sa.Column("description", sa.Text(), nullable=True))


def downgrade():
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
