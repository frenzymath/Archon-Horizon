from __future__ import annotations

"""Bind cached command receipts to their current authorization scope."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0007_idempotency_scope"
down_revision = "0006_sparse_references"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_request", sa.Column("project_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_api_request_project_id_project", "api_request", "project", ["project_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_api_request_project_id", "api_request", ["project_id"])


def downgrade() -> None:
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
