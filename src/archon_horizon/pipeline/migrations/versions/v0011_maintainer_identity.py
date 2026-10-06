"""Select the trusted final-review identity independently from PR authorship."""

from alembic import op
import sqlalchemy as sa

revision = "0011_maintainer_identity"
down_revision = "0010_child_capacity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_policy", sa.Column("maintainer_identity_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_review_policy_maintainer_identity_id_integration_identity", "review_policy", "integration_identity", ["maintainer_identity_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_review_policy_maintainer_identity_id", "review_policy", ["maintainer_identity_id"])


def downgrade() -> None:
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
