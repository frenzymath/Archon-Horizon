from __future__ import annotations

"""Protect immutable ownership and provenance independently of API validation."""

from alembic import op

revision = "0002_immutable_fields"
down_revision = "0001_pipeline"
branch_labels = None
depends_on = None


FIELDS = {
    "project": ("slug", "number"),
    "repository": ("project_id", "slug", "integration_id", "remote_id"),
    "mission": ("project_id", "number"),
    "run": ("mission_id", "phase", "number"),
    "assignment": ("run_id", "mission_id", "parent_id", "automation_id", "number"),
    "execution": (
        "assignment_id", "number", "host_id", "workspace_id", "harness_id",
        "assignment_revision_id", "mission_revision_id", "harness_revision_id",
        "skill_bundle_artifact_id", "roadmap_snapshot_id", "sandbox_manifest_artifact_id",
    ),
    "provider_thread": (
        "assignment_id", "number", "kind", "parent_request_id", "workspace_id",
        "harness_revision_id", "skill_bundle_artifact_id", "predecessor_id",
    ),
    "provider_request": (
        "provider_thread_id", "execution_id", "number", "reason",
        "reviewer_descriptor_id", "reviewer_descriptor_revision_id",
        "guidance_manifest_artifact_id", "input_artifact_id",
    ),
    "obligation": ("assignment_id", "created_by_execution_id", "number"),
    "forge_item": ("repository_id", "remote_number", "kind", "origin_run_id"),
    "host": ("slug",),
    "harness": ("slug",),
    "principal": ("kind", "username", "host_id", "execution_id", "service_name"),
    "credential": ("principal_id", "kind", "token_hash", "display_prefix"),
}


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION horizon_immutable_fields() RETURNS trigger AS $$
        DECLARE field_name text;
        BEGIN
            FOREACH field_name IN ARRAY TG_ARGV LOOP
                IF to_jsonb(OLD)->field_name IS DISTINCT FROM to_jsonb(NEW)->field_name THEN
                    RAISE EXCEPTION 'immutable field %.% cannot be changed', TG_TABLE_NAME, field_name
                        USING ERRCODE = '23514';
                END IF;
            END LOOP;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    for name, fields in FIELDS.items():
        arguments = ",".join("'" + field + "'" for field in ("id", "created_at", *fields))
        op.execute(
            f"CREATE TRIGGER immutable_fields BEFORE UPDATE ON {name} "
            f"FOR EACH ROW EXECUTE FUNCTION horizon_immutable_fields({arguments})"
        )


def downgrade() -> None:
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
