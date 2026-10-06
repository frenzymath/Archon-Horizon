"""Retain committed roadmap content and labels in a rebuildable projection."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0016_source_projection"
down_revision = "0015_forge_change"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("source_projection",
        sa.Column("repository_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("repository.id"), primary_key=True),
        sa.Column("source_path", sa.Text(), primary_key=True),
        sa.Column("source_commit_oid", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))


def downgrade():
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
