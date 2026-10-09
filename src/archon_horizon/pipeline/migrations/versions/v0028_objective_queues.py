"""Objective queues, bounded recurrence and review demand; retain existing runs."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0028_objective_queues"
down_revision = "0027_review_retry_recovery"
branch_labels = None
depends_on = None


def upgrade():
    # Server defaults intentionally preserve operator policy and live ownership.
    # The launch API opts new work into objective orchestration explicitly.
    op.drop_constraint("ck_milestone_check_kind_values", "milestone_check", type_="check")
    op.create_check_constraint("ck_milestone_check_kind_values", "milestone_check", "kind IN ('route','contract','graph','proof','library')")
    op.add_column("workspace", sa.Column("cleaned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workspace", sa.Column("cleanup_error", sa.Text(), nullable=True))
    op.add_column("resource_limit", sa.Column("circuit_open", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("resource_limit", sa.Column("circuit_reason", sa.Text(), nullable=True))
    op.add_column("execution", sa.Column("native_capacity", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("run", sa.Column("objective_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_run_objective_id_document", "run", "document", ["objective_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_run_objective_id", "run", ["objective_id"])
    op.add_column("run", sa.Column("orchestration", sa.Text(), nullable=False, server_default="legacy"))
    op.create_check_constraint("ck_run_orchestration_values", "run", "orchestration IN ('legacy','objective')")
    for name, default in (("queue_policies", "'{}'::jsonb"), ("requested_phases", "'[]'::jsonb")):
        op.add_column("run", sa.Column(name, JSONB(none_as_null=True), nullable=False, server_default=sa.text(default)))
    op.add_column("run", sa.Column("auto_advance", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("run", sa.Column("phase_history", JSONB(none_as_null=True), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("run", sa.Column("pending_phase", JSONB(none_as_null=True), nullable=True))
    for name, kind in (("category", sa.String(64)), ("recurrence_key", sa.String(200)), ("pause_reason", sa.Text)):
        op.add_column("assignment", sa.Column(name, kind, nullable=True))
    op.add_column("automation", sa.Column("pause_reason", sa.Text(), nullable=True))
    op.add_column("automation", sa.Column("frontier_hash", sa.String(128), nullable=True))
    op.add_column("obligation", sa.Column("comments", JSONB(none_as_null=True), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.create_check_constraint("ck_run_queue_policies_object", "run", "jsonb_typeof(queue_policies) = 'object'")
    op.create_check_constraint("ck_run_requested_phases_array", "run", "jsonb_typeof(requested_phases) = 'array'")
    op.create_check_constraint("ck_obligation_comments_array", "obligation", "jsonb_typeof(comments) = 'array'")
    op.create_check_constraint("ck_run_phase_history_array", "run", "jsonb_typeof(phase_history) = 'array'")
    op.create_check_constraint("ck_run_pending_phase_object", "run", "pending_phase IS NULL OR jsonb_typeof(pending_phase) = 'object'")
    op.add_column("review_policy", sa.Column("specialist_mode", sa.Text(), nullable=False, server_default="required"))
    op.create_check_constraint("ck_review_policy_specialist_mode_values", "review_policy", "specialist_mode IN ('required','advisory')")
    op.create_index("uq_assignment_recurrence", "assignment", ["recurrence_key"], unique=True,
        postgresql_where=sa.text("recurrence_key IS NOT NULL AND status IN ('pending','running','stopping')"))
    op.create_index("uq_run_live_objective", "run", ["objective_id"], unique=True,
        postgresql_where=sa.text("orchestration = 'objective' AND objective_id IS NOT NULL AND status IN ('active','paused','draining','stopping')"))
    op.create_table("review_demand",
        sa.Column("forge_item_id", UUID(as_uuid=True), sa.ForeignKey("forge_item.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("attention", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("observed_label", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("assignment_id", UUID(as_uuid=True), sa.ForeignKey("assignment.id", ondelete="RESTRICT")),
        sa.Column("handled_generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner_generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("note", sa.Text()), sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation > 0 AND handled_generation >= 0 AND handled_generation <= generation", name="ck_review_demand_generations"))
    op.create_index("ix_review_demand_run_id", "review_demand", ["run_id"])
    op.create_index("ix_review_demand_assignment_id", "review_demand", ["assignment_id"])


def downgrade():
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
