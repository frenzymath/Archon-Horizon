"""Default new harness bindings to native delegation without a Horizon cap."""

from alembic import op
import sqlalchemy as sa

revision = "0030_optional_subagent_limits"
down_revision = "0029_agent_planning"
branch_labels = None
depends_on = None


def upgrade():
    # Existing zero/positive settings and execution reservations are deliberate
    # operator state. Allow None for new settings without rewriting those rows.
    op.alter_column("host_harness", "max_parallel_subagents",
                    existing_type=sa.BigInteger(), nullable=True)
    op.alter_column("execution", "native_capacity",
                    existing_type=sa.BigInteger(), nullable=True)
    # New automatic outage guards need no implicit provider concurrency quota.
    # Keep existing account quotas intact, including earlier automatic guards.
    op.alter_column("resource_limit", "max_concurrent",
                    existing_type=sa.BigInteger(), nullable=True)
    op.create_check_constraint("ck_resource_limit_build_capacity", "resource_limit",
                               "kind = 'provider_account' OR max_concurrent IS NOT NULL")


def downgrade():
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
