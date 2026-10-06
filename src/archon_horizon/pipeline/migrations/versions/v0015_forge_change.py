"""Durable scoped file changes on deterministic new Forge branches."""

from alembic import op

revision = "0015_forge_change"
down_revision = "0014_stop_confirmation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_outbox_operation_kind_values", "outbox_operation", type_="check")
    op.create_check_constraint("ck_outbox_operation_kind_values", "outbox_operation",
        "kind IN ('provider_input','goal_update','zulip_post','forge_label','forge_review',"
        "'forge_merge','forge_create','forge_comment','forge_change','publication','worker_event')")


def downgrade() -> None:
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
