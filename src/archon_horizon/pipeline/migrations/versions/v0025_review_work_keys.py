"""Give each exact-head reviewer contract one durable owner."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0025_review_work_keys"
down_revision = "0024_mission_contracts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("review_work",
        sa.Column("forge_item_id", UUID(as_uuid=True), sa.ForeignKey("forge_item.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("head_commit_oid", sa.Text(), primary_key=True),
        sa.Column("reviewer_descriptor_revision_id", UUID(as_uuid=True), sa.ForeignKey("record_revision.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("policy_revision_id", UUID(as_uuid=True), sa.ForeignKey("record_revision.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("target_branch", sa.Text(), primary_key=True),
        sa.Column("provider_request_id", UUID(as_uuid=True), sa.ForeignKey("provider_request.id", ondelete="RESTRICT")),
        sa.Column("assignment_id", UUID(as_uuid=True), sa.ForeignKey("assignment.id", ondelete="RESTRICT")),
        sa.Column("obligation_id", UUID(as_uuid=True), sa.ForeignKey("obligation.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("attempts", sa.BigInteger(), nullable=False, server_default="1"),
        sa.CheckConstraint("attempts BETWEEN 1 AND 3", name="ck_review_work_attempt_budget"),
        sa.CheckConstraint("(provider_request_id IS NULL) <> (assignment_id IS NULL)", name="ck_review_work_one_owner"))
    for field in ("provider_request_id", "assignment_id", "obligation_id"):
        op.create_index("ix_review_work_" + field, "review_work", [field])


def downgrade():
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
