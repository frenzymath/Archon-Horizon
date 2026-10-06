"""Allow explicit repaired review retries without a lifetime attempt ceiling."""

from alembic import op


revision = "0027_review_retry_recovery"
down_revision = "0026_orchestrator_health"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_review_work_attempt_budget", "review_work", type_="check")
    op.create_check_constraint("ck_review_work_positive_attempts", "review_work", "attempts >= 1")


def downgrade():
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
