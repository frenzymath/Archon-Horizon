"""Durable Forge creation and comments, with write-once observed provenance."""

from alembic import op

revision = "0008_forge_creation"
down_revision = "0007_idempotency_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_outbox_operation_kind_values", "outbox_operation", type_="check")
    op.create_check_constraint("ck_outbox_operation_kind_values", "outbox_operation",
        "kind IN ('provider_input','goal_update','zulip_post','forge_label','forge_review',"
        "'forge_merge','forge_create','forge_comment','publication','worker_event')")
    op.execute("DROP TRIGGER immutable_fields ON forge_item")
    op.execute("CREATE TRIGGER immutable_fields BEFORE UPDATE ON forge_item FOR EACH ROW "
               "EXECUTE FUNCTION horizon_immutable_fields('id','created_at','repository_id','remote_number','kind')")
    op.execute("""
        CREATE FUNCTION horizon_forge_origin_once() RETURNS trigger AS $$
        BEGIN
            IF OLD.origin_run_id IS NOT NULL AND NEW.origin_run_id IS DISTINCT FROM OLD.origin_run_id THEN
                RAISE EXCEPTION 'Forge origin is write-once' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("CREATE TRIGGER immutable_origin BEFORE UPDATE ON forge_item "
               "FOR EACH ROW EXECUTE FUNCTION horizon_forge_origin_once()")


def downgrade() -> None:
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
