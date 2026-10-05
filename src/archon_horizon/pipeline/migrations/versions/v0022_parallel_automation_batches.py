"""Allow one recurrence policy to queue several bounded batches."""

from alembic import op
import sqlalchemy as sa


revision = "0022_parallel_automation_batches"
down_revision = "0021_usage_observations"
branch_labels = None
depends_on = None


def upgrade():
    # v0001 made the automation id a live-assignment mutex.  It prevented the
    # scheduler from keeping maintainers busy on independent review batches.
    op.drop_index("uq_assignment_live_automation", table_name="assignment")
    op.create_index(
        "ix_assignment_live_automation",
        "assignment",
        ["automation_id"],
        unique=False,
        postgresql_where=sa.text(
            "automation_id IS NOT NULL AND status IN ('pending','running','stopping')"
        ),
    )


def downgrade():
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
