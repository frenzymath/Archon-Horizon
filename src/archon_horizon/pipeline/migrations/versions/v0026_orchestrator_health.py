"""Persist orchestrator health evidence and compare-and-swap control plans."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0026_orchestrator_health"
down_revision = "0025_review_work_keys"
branch_labels = None
depends_on = None


def _scope_check(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        "num_nonnulls(project_id,run_id,host_id,assignment_id) > 0",
        name=name,
    )


def upgrade():
    op.create_table(
        "health_issue",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="RESTRICT")),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="RESTRICT")),
        sa.Column("host_id", UUID(as_uuid=True), sa.ForeignKey("host.id", ondelete="RESTRICT")),
        sa.Column("assignment_id", UUID(as_uuid=True), sa.ForeignKey("assignment.id", ondelete="RESTRICT")),
        sa.Column("dedupe_key", sa.String(256), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False, server_default="warning"),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("details", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("occurrences", sa.BigInteger(), nullable=False, server_default="1"),
        sa.CheckConstraint("revision > 0", name="ck_health_issue_positive_revision"),
        _scope_check("ck_health_issue_scope"),
        sa.CheckConstraint("severity IN ('info','warning','error','critical')", name="ck_health_issue_severity_values"),
        sa.CheckConstraint("status IN ('open','acknowledged','resolved','suppressed')", name="ck_health_issue_status_values"),
        sa.CheckConstraint("occurrences > 0", name="ck_health_issue_occurrences"),
        sa.CheckConstraint("jsonb_typeof(details) = 'object'", name="ck_health_issue_details_object"),
        sa.UniqueConstraint("dedupe_key", name="uq_health_issue_dedupe_key"),
    )
    op.create_index("ix_health_issue_run_status", "health_issue", ["run_id", "status"])
    op.create_index("ix_health_issue_host_status", "health_issue", ["host_id", "status"])

    op.create_table(
        "health_snapshot",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="RESTRICT")),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="RESTRICT")),
        sa.Column("host_id", UUID(as_uuid=True), sa.ForeignKey("host.id", ondelete="RESTRICT")),
        sa.Column("kind", sa.Text(), nullable=False, server_default="run"),
        sa.Column("dedupe_key", sa.String(256), nullable=False),
        sa.Column("frontier_hash", sa.String(128), nullable=False),
        sa.Column("health_hash", sa.String(128), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("num_nonnulls(project_id,run_id,host_id) > 0", name="ck_health_snapshot_scope"),
        sa.CheckConstraint("kind IN ('system','run','host')", name="ck_health_snapshot_kind_values"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_health_snapshot_payload_object"),
        sa.UniqueConstraint("dedupe_key", name="uq_health_snapshot_dedupe_key"),
    )
    op.create_index("ix_health_snapshot_run_captured", "health_snapshot", ["run_id", "captured_at"])

    op.create_table(
        "control_plan",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_by_execution_id", UUID(as_uuid=True), sa.ForeignKey("execution.id", ondelete="RESTRICT")),
        sa.Column("dedupe_key", sa.String(256), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="proposed"),
        sa.Column("expected_run_revision", sa.BigInteger(), nullable=False),
        sa.Column("expected_frontier_hash", sa.String(128)),
        sa.Column("actions", JSONB(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.Column("rejected_at", sa.DateTime(timezone=True)),
        sa.Column("decision_note", sa.Text()),
        sa.CheckConstraint("revision > 0", name="ck_control_plan_positive_revision"),
        sa.CheckConstraint("expected_run_revision > 0", name="ck_control_plan_expected_run_revision"),
        sa.CheckConstraint("status IN ('proposed','accepted','applied','rejected','superseded','expired')", name="ck_control_plan_status_values"),
        sa.CheckConstraint("jsonb_typeof(actions) = 'object'", name="ck_control_plan_actions_object"),
        sa.UniqueConstraint("dedupe_key", name="uq_control_plan_dedupe_key"),
    )
    op.create_index("ix_control_plan_run_status", "control_plan", ["run_id", "status"])


def downgrade():
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
