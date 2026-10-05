"""Pin one immutable review manifest to an ordinary scheduled assignment."""

from alembic import op

revision = "0013_reviewer_assignment"
down_revision = "0012_reference_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE assignment_artifact ADD COLUMN purpose text NOT NULL DEFAULT 'evidence' "
               "CHECK (purpose IN ('evidence', 'reviewer_manifest'))")
    op.execute("CREATE UNIQUE INDEX uq_assignment_reviewer_manifest ON assignment_artifact (assignment_id) "
               "WHERE purpose = 'reviewer_manifest'")
    op.execute("CREATE TRIGGER immutable_review_manifest BEFORE UPDATE ON assignment_artifact "
               "FOR EACH ROW WHEN (OLD.purpose = 'reviewer_manifest' OR NEW.purpose = 'reviewer_manifest') "
               "EXECUTE FUNCTION horizon_immutable_record()")


def downgrade() -> None:
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
