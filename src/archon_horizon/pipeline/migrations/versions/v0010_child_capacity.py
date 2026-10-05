"""Track child provider reservations and explicit delivery cancellation."""

from alembic import op
import sqlalchemy as sa

revision = "0010_child_capacity"
down_revision = "0009_native_background"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("resource_claim", sa.Column("provider_request_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_resource_claim_provider_request_id_provider_request", "resource_claim", "provider_request", ["provider_request_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_resource_claim_provider_request_id", "resource_claim", ["provider_request_id"])
    op.drop_index("uq_resource_claim_active", table_name="resource_claim")
    op.create_index("uq_resource_claim_active", "resource_claim", ["resource_limit_id", "execution_id"], unique=True,
                    postgresql_where=sa.text("released_at IS NULL AND provider_request_id IS NULL"))
    op.create_index("uq_resource_claim_child", "resource_claim", ["resource_limit_id", "provider_request_id"], unique=True,
                    postgresql_where=sa.text("released_at IS NULL AND provider_request_id IS NOT NULL"))
    op.drop_constraint("ck_outbox_operation_status_values", "outbox_operation", type_="check")
    op.create_check_constraint("ck_outbox_operation_status_values", "outbox_operation",
                               "status IN ('pending','running','completed','failed','uncertain','cancelled')")


def downgrade() -> None:
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
