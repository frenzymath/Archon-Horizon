"""Preserve coordination episodes across scheduler and provider restarts."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0023_run_coordination"
down_revision = "0022_parallel_automation_batches"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("run_coordination",
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("frontier_hash", sa.Text(), nullable=False),
        sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("audit_obligation_id", UUID(as_uuid=True), sa.ForeignKey("obligation.id", ondelete="RESTRICT")),
        sa.Column("audit_frontier_hash", sa.Text()))
    op.create_index("ix_run_coordination_audit_obligation_id", "run_coordination", ["audit_obligation_id"])


def downgrade():
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
