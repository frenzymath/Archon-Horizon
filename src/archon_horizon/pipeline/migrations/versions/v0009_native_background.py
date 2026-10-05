"""Distinguish native background-launch acknowledgements from completion."""

from alembic import op
import sqlalchemy as sa

revision = "0009_native_background"
down_revision = "0008_forge_creation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("provider_request", sa.Column("native_background", sa.Boolean(), nullable=True))
    op.execute("""
        CREATE FUNCTION horizon_native_background_once() RETURNS trigger AS $$
        BEGIN
            IF OLD.native_background IS NOT NULL AND NEW.native_background IS DISTINCT FROM OLD.native_background THEN
                RAISE EXCEPTION 'Native invocation mode is write-once' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("CREATE TRIGGER immutable_native_background BEFORE UPDATE ON provider_request "
               "FOR EACH ROW EXECUTE FUNCTION horizon_native_background_once()")


def downgrade() -> None:
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
