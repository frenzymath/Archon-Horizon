"""Give mission delegation an explicit scope, acceptance contract and budget."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0024_mission_contracts"
down_revision = "0023_run_coordination"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("mission", sa.Column("acceptance_criteria", ARRAY(sa.Text()),
        nullable=False, server_default=sa.text("'{}'::text[]")))
    op.add_column("mission", sa.Column("delegation_note", sa.Text()))
    op.add_column("mission", sa.Column("scope", JSONB(), nullable=False,
        server_default=sa.text("'{}'::jsonb")))
    op.add_column("mission", sa.Column("max_open_children", sa.BigInteger(),
        nullable=False, server_default="8"))
    op.create_check_constraint("ck_mission_child_budget", "mission", "max_open_children > 0")
    op.create_check_constraint("ck_mission_scope_object", "mission", "jsonb_typeof(scope) = 'object'")


def downgrade():
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
