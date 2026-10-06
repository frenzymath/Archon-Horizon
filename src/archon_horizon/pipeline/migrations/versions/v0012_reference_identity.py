"""Canonical project-scoped bibliographic identity, retaining explicit citation versions."""

from alembic import op

revision = "0012_reference_identity"
down_revision = "0011_maintainer_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for kind in ("doi", "arxiv", "isbn", "pmid"):
        op.drop_index(f"ix_reference_{kind}", table_name="reference")
        value = f"(identifiers ->> '{kind}')"
        if kind == "arxiv":
            value = f"regexp_replace(regexp_replace({value}, 'v[1-9][0-9]*$', ''), '\\.[A-Z]{{2}}/', '/')"
        op.execute(f"CREATE UNIQUE INDEX uq_reference_{kind} ON reference (project_id, ({value})) "
                   f"WHERE identifiers ->> '{kind}' IS NOT NULL")


def downgrade() -> None:
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
