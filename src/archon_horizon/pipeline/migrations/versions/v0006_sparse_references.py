from __future__ import annotations

"""Index only populated typed references instead of every nullable union arm."""

import sqlalchemy as sa
from alembic import op

revision = "0006_sparse_references"
down_revision = "0005_forge_outbox"
branch_labels = None
depends_on = None

TARGETS = (
    "project", "mission", "run", "assignment", "execution", "provider_thread", "provider_request",
    "automation", "obligation", "activity", "node", "document", "reference", "roadmap_snapshot",
    "review_policy", "reviewer_descriptor", "integration_identity", "review_gate", "forge_review",
    "forge_item", "discussion", "message", "artifact", "publication", "host", "harness", "workspace", "verification",
)


def upgrade() -> None:
    for target in TARGETS:
        op.drop_index(f"ix_object_reference_{target}_id", table_name="object_reference")
        op.drop_index(f"uq_object_reference_{target}", table_name="object_reference")
        op.create_index(f"uq_object_reference_{target}", "object_reference", [f"{target}_id"], unique=True,
                        postgresql_where=sa.text(f"{target}_id IS NOT NULL"))


def downgrade() -> None:
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
