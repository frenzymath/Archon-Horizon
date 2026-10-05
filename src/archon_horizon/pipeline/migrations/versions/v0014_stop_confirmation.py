"""Distinguish expired ownership from observed physical process cleanup."""

from alembic import op

revision = "0014_stop_confirmation"
down_revision = "0013_reviewer_assignment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE execution ADD COLUMN stop_confirmed_at timestamptz")
    op.execute("CREATE INDEX ix_execution_unconfirmed_host ON execution (host_id, harness_id, workspace_id) "
               "WHERE stop_confirmed_at IS NULL")


def downgrade() -> None:
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
