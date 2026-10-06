from __future__ import annotations

"""Make JSON lifecycle checks reject SQL NULL, not only false."""

from alembic import op

revision = "0003_resolution_checks"
down_revision = "0002_immutable_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_run_phase_kind", "run", type_="check")
    op.create_check_constraint(
        "ck_run_phase_kind", "run",
        "coalesce(phase->>'kind' IN ('preprocessing','formalization','postprocessing'), false)",
    )
    op.drop_constraint("ck_obligation_resolution_status", "obligation", type_="check")
    op.create_check_constraint(
        "ck_obligation_resolution_status", "obligation",
        "(status = 'open') = (resolution IS NULL) AND coalesce("
        "(status = 'open' AND resolution IS NULL) OR "
        "(status = 'done' AND resolution->>'kind' = 'completed') OR "
        "(status = 'handled' AND resolution->>'kind' IN ('delegated','scheduled','reconsider')) OR "
        "(status = 'superseded' AND resolution->>'kind' = 'superseded'), false)",
    )


def downgrade() -> None:
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
