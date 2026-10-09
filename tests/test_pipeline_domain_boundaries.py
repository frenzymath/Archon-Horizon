"""Behavioral regressions found by the project/operations/dashboard review."""

import base64
import json
from uuid import uuid4

import pytest

from archon_horizon.pipeline.config import PipelineConfig
from archon_horizon.pipeline.dashboard import dashboard_activity, readmodels
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.operations import storage
from archon_horizon.pipeline.operations.health_control import (
    HealthIssueSpec, HealthSnapshotSpec, Scope, capture_health_snapshot, upsert_health_issue,
)
from archon_horizon.pipeline.persistence.records import change, create, get
from archon_horizon.pipeline.projects.catalog import CatalogUpdate, update_catalog
from archon_horizon.pipeline.projects.documents import parse_document
from test_pipeline_provider_events import start
from test_pipeline_search import manager, next_commit, source, wait_for  # noqa: F401
from test_pipeline_service import service_database, world  # noqa: F401


@pytest.mark.parametrize("value", ["false", "0", "[]", "''", "value: .nan", "value: .inf", "value: -.inf"])
def test_frontmatter_rejects_nonobjects_and_nonfinite_numbers(value):
    with pytest.raises(ValueError):
        parse_document("---\n" + value + "\n---\nBody\n")
    assert parse_document("---\n---\nBody\n") == ({}, "Body\n")


@pytest.mark.parametrize("marker", [[], None, False, 1, "invalid"])
def test_malformed_retention_records_are_preserved_without_breaking_cleanup(tmp_path, marker):
    config = PipelineConfig(database_url="postgresql+psycopg://local/test", state_root=tmp_path)
    diagnostic = tmp_path / "diagnostics" / "unrecognized"
    backup = tmp_path / "backups" / "unrecognized"
    diagnostic.mkdir(parents=True)
    backup.mkdir(parents=True)
    (diagnostic / "retention.json").write_text(json.dumps(marker))
    (backup / "manifest.json").write_text(json.dumps(marker))
    manager = storage.StorageManager(config)
    assert manager.cleanup_preview()["candidates"] == []
    assert manager.cleanup({"candidates": []})["removed"] == []
    assert diagnostic.is_dir() and backup.is_dir()
    with pytest.raises(ValueError, match="unsupported backup manifest"):
        storage.verify_backup(backup)


def test_health_scope_rejects_an_assignment_from_another_run_in_the_same_project(world):
    run, other_run = world.run(), world.run()
    assignment = world.assignment(other_run)
    spec = HealthIssueSpec(scope=Scope(run_id=run["id"], assignment_id=assignment["id"]),
                           code="scope.check", fingerprint="wrong-run", summary="Wrong owner scope")
    with pytest.raises(DomainError) as error:
        upsert_health_issue(world.conn, spec)
    assert error.value.code == "scope_mismatch"
    matching = spec.model_copy(update={"scope": Scope(run_id=other_run["id"], assignment_id=assignment["id"])})
    assert upsert_health_issue(world.conn, matching)["assignment_id"] == assignment["id"]


def test_workflow_change_waits_for_a_stopping_run_to_settle(world):
    run = world.run()
    change(world.conn, "run", run["id"], status="stopping")
    project = get(world.conn, "project", world.project["id"])
    with pytest.raises(DomainError) as error:
        update_catalog(world.conn, world.actor, "project", project["id"], CatalogUpdate(
            expected_revision=project["revision"], changes={"workflow": "legacy"}))
    assert error.value.code == "active_project_runs"
    assert get(world.conn, "project", project["id"])["workflow"] == project["workflow"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_health_payload_rejects_nonfinite_json_before_database_write(value):
    scope = Scope(run_id=uuid4())
    with pytest.raises(DomainError) as error:
        capture_health_snapshot(None, HealthSnapshotSpec(scope=scope, frontier_hash="frontier", health_hash="health",
            snapshot_key="nonfinite", payload={"nested": [value]}))
    assert error.value.code == "invalid_health_payload"


def test_snapshot_contract_excludes_assignment_scope_not_supported_by_storage():
    with pytest.raises(ValueError, match="assignment observations use health issues"):
        HealthSnapshotSpec(scope=Scope(assignment_id=uuid4()), frontier_hash="frontier", health_hash="health",
                           snapshot_key="assignment", payload={})


@pytest.mark.parametrize("identity", [3, True, [], {}])
def test_malformed_cursor_id_returns_a_domain_error(world, identity):
    cursor = base64.urlsafe_b64encode(json.dumps({"id": identity, "value": 1}).encode()).decode().rstrip("=")
    with pytest.raises(DomainError) as error:
        readmodels.runs(world.conn, world.actor, world.project["id"], cursor, 10)
    assert error.value.code == "invalid_cursor"


def test_queue_positions_survive_pagination_and_individual_session_filtering(world):
    run = world.run()
    world.disable_automations(run)
    assignments = [world.assignment(run) for _ in range(4)]
    expected = {assignment["id"]: number for number, assignment in enumerate(assignments, 1)}
    for compact in (True, False):
        first = dashboard_activity.run_detail(world.conn, world.actor, run["id"], service=world.service,
                                             compact=compact, sessions_limit=2)
        second = dashboard_activity.run_detail(world.conn, world.actor, run["id"], service=world.service,
            compact=compact, sessions_limit=2, sessions_before=first["sessions_next_before"])
        for session in [*first["sessions"], *second["sessions"]]:
            assert session["queue_position"] == expected[session["id"]]
    session = dashboard_activity.session_detail(world.conn, world.actor, assignments[-1]["id"],
        service=world.service, store=world.service.store, compact=True)
    assert session["queue_position"] == 4


def test_session_usage_keeps_the_uncertainty_of_primary_threads(world):
    execution, thread, _ = start(world)
    assignment = get(world.conn, "assignment", execution["assignment_id"])
    create(world.conn, "usage_record", execution_id=execution["id"], provider_thread_id=thread["id"],
           provider_record_id="codex:legacy-total", input_tokens=12, output_tokens=4)
    session = dashboard_activity.session_detail(world.conn, world.actor, assignment["id"],
        service=world.service, store=world.service.store, compact=True)
    assert session["usage"]["tokens_in"] is None
    assert session["usage"]["incomplete"] is True


def test_failed_search_retirement_releases_cleanup_fence_and_failed_index(tmp_path, source, monkeypatch):
    from archon_horizon.pipeline.projects import search as search_module

    with manager(tmp_path) as previous:
        assert wait_for(previous, source)["status"] == "ready"
        newer = next_commit(source, tmp_path)
        assert wait_for(previous, newer)["status"] == "ready"
        old_key, new_key = previous.generation_key(source), previous.generation_key(newer)
    with manager(tmp_path) as search:
        search.disk_budget_bytes = search._disk[new_key]
        remove = search_module.shutil.rmtree
        failures = []

        def fail_old_retirement(path, *args, **kwargs):
            if path == search.generations / old_key and not failures:
                failures.append(path)
                raise PermissionError("transient generation deletion failure")
            return remove(path, *args, **kwargs)

        monkeypatch.setattr(search_module.shutil, "rmtree", fail_old_retirement)
        assert wait_for(search, newer)["status"] == "failed"
        assert failures
        assert not search._evicting
        assert search.stats()["memory_bytes"] == 0
        assert search._entries[new_key].index is None
        search.disk_budget_bytes = 4 * 1024**2
        assert wait_for(search, source)["status"] == "ready"
