from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from archon_horizon.pipeline.persistence.database import Database
from archon_horizon.pipeline.persistence.schema import metadata, tables


@pytest.fixture(scope="module")
def database():
    url = os.environ.get("HORIZON_PIPELINE_TEST_URL")
    if not url:
        pytest.skip("set HORIZON_PIPELINE_TEST_URL to an isolated PostgreSQL test database")
    schema = "pipeline_test_" + uuid4().hex
    db = Database(url, schema=schema)
    db.migrate()
    try:
        yield db
    finally:
        with db.engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        db.close()


@pytest.fixture
def conn(database):
    with database.engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def put(conn, table_name: str, **values):
    return conn.execute(insert(tables[table_name]).values(**values).returning(tables[table_name].c.id)).scalar_one()


@pytest.fixture
def graph(conn):
    now = datetime.now(timezone.utc)
    principal = put(conn, "principal", kind="service", display_name="Tests", service_name="test")
    project = put(conn, "project", slug="proofs", title="Proofs")
    integration = put(conn, "integration", kind="forge", endpoint="https://forge.example", credential_ref="secret:test")
    repo = put(conn, "repository", project_id=project, slug="workspace", integration_id=integration, remote_id="1", default_branch="main", purpose="workspace")
    document = put(conn, "document", project_id=project, number=1, kind="roadmap", title="Plan", source_repository_id=repo, source_path="README.md", source_commit_oid="a" * 40)
    mission = put(conn, "mission", project_id=project, number=1, title="Prove", objective="Prove the target")
    host = put(conn, "host", slug="local", display_name="Local", workspace_root="/work", scratch_root="/scratch", sandbox={"mode": "unrestricted"})
    harness = put(conn, "harness", slug="codex", adapter="codex_exec", adapter_version="1", provider_version="1", model_options={}, settings={})
    conn.execute(insert(tables["host_harness"]).values(host_id=host, harness_id=harness, executable_path="/bin/codex", provider_home="/provider", credential_ref="secret:provider", execution_slots=2, max_parallel_subagents=4))
    workspace = put(conn, "workspace", project_id=project, host_id=host, repository_id=repo, path="/work/one", branch_name="main", base_commit_oid="a" * 40, status="ready")
    artifact = put(conn, "artifact", project_id=project, kind="blob", content={"sha256": "a" * 64, "size_bytes": 1, "media_type": "application/json"})
    run = put(conn, "run", mission_id=mission, phase={"kind": "preprocessing", "roadmap_document_id": str(document)}, retry_policy={"max_recovery_attempts": 3})
    assignment = put(conn, "assignment", run_id=run, number=1, mission_id=mission, queue_rank=100)
    revisions = {}
    for kind, ident in (("mission", mission), ("harness", harness), ("assignment", assignment)):
        ref = put(conn, "object_reference", kind=kind, **{kind + "_id": ident})
        revisions[kind] = put(conn, "record_revision", object_id=ref, object_revision=1, schema_version=1, actor_principal_id=principal, content={"kind": kind})
    execution_values = dict(assignment_id=assignment, number=1, host_id=host, workspace_id=workspace, harness_id=harness,
                            assignment_revision_id=revisions["assignment"], mission_revision_id=revisions["mission"], harness_revision_id=revisions["harness"],
                            skill_bundle_artifact_id=artifact, sandbox_manifest_artifact_id=artifact, lease_expires_at=now + timedelta(minutes=5))
    execution = put(conn, "execution", **execution_values)
    return dict(project=project, principal=principal, mission=mission, run=run, host=host, harness=harness,
                workspace=workspace, artifact=artifact, assignment=assignment, execution=execution,
                execution_values=execution_values, revisions=revisions)


def test_database_requires_explicit_postgres_and_valid_schema():
    for url in ("sqlite://", "postgresql+psycopg://localhost"):
        with pytest.raises(ValueError):
            Database(url)
    with pytest.raises(ValueError):
        Database("postgresql://localhost/test", schema="public;drop database test")


def test_full_migration_is_explicit_repeatable_and_current(database):
    database.migrate()
    assert database.check_revision() == "0031_reference_files"
    with database.transaction() as conn:
        names = set(conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema = :schema"),
                                 {"schema": database.schema}).scalars())
        assert names == set(metadata.tables) | {"alembic_version"}


def test_optional_subagent_migration_preserves_existing_limits_and_defaults_new_bindings():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.schema import DropSchema
    from archon_horizon.pipeline._resources import MIGRATIONS_ROOT

    url = os.environ.get("HORIZON_PIPELINE_TEST_URL")
    if not url:
        pytest.skip("set HORIZON_PIPELINE_TEST_URL to an isolated PostgreSQL test database")
    schema = "subagent_migration_" + uuid4().hex
    db = Database(url, schema=schema)
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_ROOT))
    config.attributes["schema"] = schema
    try:
        with db.engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config.attributes["connection"] = conn
            command.upgrade(config, "0029_agent_planning")
            host = put(conn, "host", slug="migration-host", display_name="Migration host",
                workspace_root="/work", scratch_root="/scratch", sandbox={"mode": "unrestricted"})
            for limit in (0, 3):
                harness = put(conn, "harness", slug=f"migration-harness-{limit}", adapter="codex_exec",
                    adapter_version="1", provider_version="1", model_options={}, settings={})
                conn.execute(insert(tables["host_harness"]).values(host_id=host, harness_id=harness,
                    executable_path="/bin/codex", provider_home="/provider", credential_ref="secret:provider",
                    execution_slots=1, max_parallel_subagents=limit))
            quota = put(conn, "resource_limit", kind="provider_account", slug="existing-account", max_concurrent=2)
        db.migrate()
        with db.transaction() as conn:
            binding = tables["host_harness"]
            assert list(conn.execute(select(binding.c.max_parallel_subagents)
                .order_by(binding.c.max_parallel_subagents)).scalars()) == [0, 3]
            harness = put(conn, "harness", slug="migration-harness-default", adapter="codex_exec",
                adapter_version="1", provider_version="1", model_options={}, settings={})
            value = conn.execute(insert(binding).values(host_id=host, harness_id=harness,
                executable_path="/bin/codex", provider_home="/provider", credential_ref="secret:provider",
                execution_slots=1).returning(binding.c.max_parallel_subagents)).scalar_one()
            assert value is None
            assert conn.execute(select(tables["resource_limit"].c.max_concurrent)
                .where(tables["resource_limit"].c.id == quota)).scalar_one() == 2
            put(conn, "resource_limit", kind="provider_account", slug="uncapped-guard", max_concurrent=None)
            with pytest.raises(IntegrityError), conn.begin_nested():
                put(conn, "resource_limit", kind="build_pool", slug="invalid-build-pool", max_concurrent=None)
    finally:
        with db.engine.begin() as conn:
            conn.execute(DropSchema(schema, cascade=True, if_exists=True))
        db.close()


def test_revision_timestamp_and_immutable_identity(conn, graph):
    project = tables["project"]
    before = conn.execute(select(project).where(project.c.id == graph["project"])).mappings().one()
    conn.execute(update(project).where(project.c.id == graph["project"]).values(title="New title"))
    after = conn.execute(select(project).where(project.c.id == graph["project"])).mappings().one()
    assert after["revision"] == before["revision"] + 1
    assert after["updated_at"] >= before["updated_at"]
    assert after["updated_at"].tzinfo is not None
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(update(project).where(project.c.id == graph["project"]).values(slug="new-slug"))


def test_history_and_run_initial_baseline_cannot_be_rewritten(conn, graph):
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(update(tables["record_revision"]).values(content={"replaced": True}))
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(update(tables["run"]).where(tables["run"].c.id == graph["run"]).values(phase={"kind": "preprocessing", "roadmap_document_id": str(uuid4())}))


def test_forge_sync_before_dispatch_settlement_allows_origin_once(conn, graph):
    repository_id = conn.execute(select(tables["workspace"].c.repository_id).where(
        tables["workspace"].c.id == graph["workspace"])).scalar_one()
    item_id = put(conn, "forge_item", repository_id=repository_id, remote_number=1, kind="issue",
                  title="Observed before queue receipt", status="open", observed_at=datetime.now(timezone.utc))
    items = tables["forge_item"]
    conn.execute(update(items).where(items.c.id == item_id).values(origin_run_id=graph["run"], review_phase="preprocessing"))
    assert conn.execute(select(items.c.origin_run_id).where(items.c.id == item_id)).scalar_one() == graph["run"]
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(update(items).where(items.c.id == item_id).values(origin_run_id=None))


def test_forge_file_change_is_a_durable_idempotent_outbox_kind(conn, graph):
    repository_id = conn.execute(select(tables["workspace"].c.repository_id).where(
        tables["workspace"].c.id == graph["workspace"])).scalar_one()
    values = {"project_id": graph["project"], "actor_principal_id": graph["principal"],
        "kind": "forge_change", "schema_version": 1, "idempotency_key": "file-change-once", "payload": {
            "repository_id": str(repository_id), "origin_run_id": str(graph["run"]), "base_commit_oid": "a" * 40,
            "message": "Update roadmap", "files": [{"operation": "update", "path": "roadmap.md",
                "sha": "b" * 40, "content_artifact_id": str(graph["artifact"])}]}}
    identifier = put(conn, "outbox_operation", **values)
    row = conn.execute(select(tables["outbox_operation"]).where(tables["outbox_operation"].c.id == identifier)).mappings().one()
    assert row["kind"] == "forge_change" and row["status"] == "pending"
    assert row["payload"] == values["payload"]
    with pytest.raises(IntegrityError), conn.begin_nested():
        put(conn, "outbox_operation", **values)


def test_object_refs_have_real_foreign_keys_and_exactly_one_target(conn, graph):
    with pytest.raises(IntegrityError), conn.begin_nested():
        put(conn, "object_reference", kind="project", project_id=uuid4())
    with pytest.raises(IntegrityError), conn.begin_nested():
        put(conn, "object_reference", kind="project", project_id=graph["project"], mission_id=graph["mission"])
    with pytest.raises(IntegrityError), conn.begin_nested():
        put(conn, "object_reference", kind="project", mission_id=graph["mission"])


def test_only_one_live_execution_per_assignment_and_workspace(conn, graph):
    with pytest.raises(IntegrityError), conn.begin_nested():
        put(conn, "execution", **{**graph["execution_values"], "number": 2})
    conn.execute(update(tables["execution"]).where(tables["execution"].c.id == graph["execution"]).values(status="failed"))
    resumed = put(conn, "execution", **{**graph["execution_values"], "number": 2})
    assert resumed != graph["execution"]


def test_primary_thread_is_unique_but_review_children_can_run_in_parallel(conn, graph):
    values = dict(assignment_id=graph["assignment"], workspace_id=graph["workspace"],
                  harness_revision_id=graph["revisions"]["harness"], skill_bundle_artifact_id=graph["artifact"],
                  provider_state_ref="local:primary", applied_model_options={})
    primary = put(conn, "provider_thread", **values, number=1)
    with pytest.raises(IntegrityError), conn.begin_nested():
        put(conn, "provider_thread", **values, number=2)
    parent_request = put(conn, "provider_request", provider_thread_id=primary, execution_id=graph["execution"], number=1, reason="assignment")
    for number in (2, 3):
        child = put(conn, "provider_thread", **values, number=number, kind="child", parent_request_id=parent_request)
        put(conn, "provider_request", provider_thread_id=child, execution_id=graph["execution"], number=1, reason="review")
    with pytest.raises(IntegrityError), conn.begin_nested():
        put(conn, "provider_request", provider_thread_id=primary, execution_id=graph["execution"], number=2, reason="continuation")
    conn.execute(update(tables["provider_request"]).where(tables["provider_request"].c.id == parent_request).values(status="uncertain"))
    with pytest.raises(IntegrityError), conn.begin_nested():
        put(conn, "provider_request", provider_thread_id=primary, execution_id=graph["execution"], number=2, reason="continuation")


def test_parallel_recurring_batches_preserve_outbox_deduplication(conn, graph):
    automation = put(conn, "automation", run_id=graph["run"], mission_id=graph["mission"], name="planner", cooldown_seconds=30)
    values = dict(run_id=graph["run"], mission_id=graph["mission"], automation_id=automation, queue_rank=200)
    first = put(conn, "assignment", **values, number=2)
    second = put(conn, "assignment", **values, number=3)
    assert first != second
    conn.execute(update(tables["assignment"]).where(tables["assignment"].c.id == first).values(status="completed"))
    put(conn, "assignment", **values, number=4)
    operation = dict(project_id=graph["project"], actor_principal_id=graph["principal"], kind="worker_event", idempotency_key="same", schema_version=1, payload={})
    put(conn, "outbox_operation", **operation)
    with pytest.raises(IntegrityError), conn.begin_nested():
        put(conn, "outbox_operation", **operation)


def test_cancelled_assignment_preserves_open_obligations(conn, graph):
    obligation = put(conn, "obligation", assignment_id=graph["assignment"], number=1, description="Unfinished theorem", resolution=None)
    conn.execute(update(tables["assignment"]).where(tables["assignment"].c.id == graph["assignment"]).values(status="cancelled"))
    assert conn.execute(select(tables["obligation"].c.status).where(tables["obligation"].c.id == obligation)).scalar_one() == "open"


def test_settled_obligations_cannot_bypass_resolution_with_sql_null(conn, graph):
    for resolution in (None, {}, {"kind": "completed"}):
        with pytest.raises(IntegrityError), conn.begin_nested():
            put(conn, "obligation", assignment_id=graph["assignment"], number=1,
                description="Must be accounted for", status="handled", resolution=resolution)


def test_idempotency_has_one_result_without_requiring_an_artifact_project(conn, graph):
    values = dict(principal_id=graph["principal"], operation="create_host", idempotency_key="installation-create",
                  request_sha256="a" * 64, expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    request = put(conn, "api_request", **values)
    table = tables["api_request"]
    conn.execute(update(table).where(table.c.id == request).values(status="completed", response={"id": str(graph["host"])}))
    assert conn.execute(select(table.c.response).where(table.c.id == request)).scalar_one() == {"id": str(graph["host"])}
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(update(table).where(table.c.id == request).values(response_artifact_id=graph["artifact"]))
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(update(table).where(table.c.id == request).values(response=None))
