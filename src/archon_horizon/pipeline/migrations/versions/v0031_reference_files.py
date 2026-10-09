"""Attach durable source documents to bibliographic references."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0031_reference_files"
down_revision = "0030_optional_subagent_limits"
branch_labels = None
depends_on = None


def upgrade():
    # Freeze this DDL here: later changes to the live model must not rewrite
    # what an already applied migration means.
    op.create_table("reference_file",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("reference_id", UUID(as_uuid=True), sa.ForeignKey("reference.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("artifact_id", UUID(as_uuid=True), sa.ForeignKey("artifact.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("revision > 0", name="ck_reference_file_positive_revision"),
        sa.UniqueConstraint("reference_id", "artifact_id", "filename", name="uq_reference_file_reference_id"))
    op.create_index("ix_reference_file_reference_id", "reference_file", ["reference_id"])
    op.create_index("ix_reference_file_artifact_id", "reference_file", ["artifact_id"])


def downgrade():
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
