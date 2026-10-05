from __future__ import annotations

"""Durable attributed Forge reviews and exact-head merge requests."""

from alembic import op

revision = "0005_forge_outbox"
down_revision = "0004_inline_command_response"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_outbox_operation_kind_values", "outbox_operation", type_="check")
    op.create_check_constraint("ck_outbox_operation_kind_values", "outbox_operation",
        "kind IN ('provider_input','goal_update','zulip_post','forge_label','forge_review',"
        "'forge_merge','publication','worker_event')")


def downgrade() -> None:
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
