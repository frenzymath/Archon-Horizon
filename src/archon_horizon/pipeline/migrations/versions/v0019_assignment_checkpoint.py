"""Allow a running assignment to wait without replacing its retained context."""

from alembic import op
import sqlalchemy as sa

revision = "0019_assignment_checkpoint"
down_revision = "0018_subagent_description"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("assignment", sa.Column("checkpoint_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint("ck_outbox_operation_kind_values", "outbox_operation", type_="check")
    op.create_check_constraint("ck_outbox_operation_kind_values", "outbox_operation",
        "kind IN ('provider_input','goal_update','zulip_post','forge_label','forge_review','forge_merge',"
        "'forge_create','forge_comment','forge_change','forge_edit','publication','worker_event')")


def downgrade():
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
