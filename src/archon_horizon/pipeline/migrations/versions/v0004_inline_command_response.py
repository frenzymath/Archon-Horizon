from __future__ import annotations

"""Allow bounded installation command results without inventing a project."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_inline_command_response"
down_revision = "0003_resolution_checks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_request", sa.Column("response", JSONB(none_as_null=True), nullable=True))
    op.create_check_constraint("ck_api_request_response_object", "api_request",
                               "response IS NULL OR jsonb_typeof(response) = 'object'")
    op.create_check_constraint("ck_api_request_response_storage", "api_request",
                               "num_nonnulls(response,response_artifact_id) <= 1 AND "
                               "(status <> 'completed' OR num_nonnulls(response,response_artifact_id) = 1)")


def downgrade() -> None:
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
