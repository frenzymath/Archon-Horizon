"""Behavioral regressions found by the independent core implementation audit."""

from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from archon_horizon.pipeline import models
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.execution import objectives
from archon_horizon.pipeline.execution.coordination import global_health, summary
from archon_horizon.pipeline.execution.scheduler import Scheduler
from archon_horizon.pipeline.persistence.records import change, create, get, save_blob
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_api import api, api_database, auth, mutate  # noqa: F401
from test_pipeline_objectives import assignments, launch
from test_pipeline_service import service_database, world  # noqa: F401


@pytest.mark.parametrize("omitted", ["node", "document"])
def test_narrowing_mission_scope_preserves_existing_graph_links(world, omitted):
    node = create(world.conn, "node", project_id=world.project["id"], number=1,
        title="Linked node", source_repository_id=world.workspace_repo["id"],
        source_path="Math/linked.md", source_commit_oid="a" * 40)
    linked = {"node_ids": [node["id"]], "document_ids": [world.document["id"]]}
    mission = world.service.mission(world.conn, world.actor, models.MissionCreate(
        project_id=world.project["id"], title="Linked mission", objective="Preserve graph links",
        scope=linked, **linked))
    narrowed = dict(linked, **{omitted + "_ids": []})
    with pytest.raises(DomainError) as error:
        world.service.update_mission(world.conn, world.actor, mission["id"], models.MissionUpdate(
            expected_revision=mission["revision"], scope=narrowed))
    assert error.value.code == "scope_link_mismatch"
    assert get(world.conn, "mission", mission["id"])["scope"] == mission["scope"]


def test_coordination_capacity_includes_reserved_native_children(world):
    run = launch(world)
    change(world.conn, "assignment", assignments(world, run)[0]["id"], status="cancelled")
    world.conn.execute(update(tables["host_harness"]).where(
        tables["host_harness"].c.host_id == world.host["id"]).values(execution_slots=4))
    world.conn.execute(update(tables["resource_limit"]).values(max_concurrent=4))
    mission = objectives.child_mission(world.scheduler, world.conn, run,
        "Bounded proof", "Prove the scoped lemma", ["Checked proof"])
    item = world.service.assignment(world.conn, world.actor, models.AssignmentCreate(
        run_id=run["id"], mission_id=mission["id"]))
    grant = world.claim()
    assert grant["assignment_id"] == str(item["id"])
    assert grant["max_parallel_subagents"] == 2
    pools = summary(world.conn, world.service, run)["healthy_capacity_pools"]
    pool = next(row for row in pools if row["host_id"] == world.host["id"])
    assert (pool["occupied"], pool["free_slots"]) == (3, 1)


def test_objective_planner_successor_is_expected_recurrence_health(world):
    run = launch(world)
    world.claim()
    assert len(assignments(world, run)) == 2
    health = global_health(world.conn, world.service)
    assert health["duplicate_automations"] == 0
    assert health["status"] == "healthy"
    assert health["queue_policy"]["objective_planner_max_outstanding"] == 2


def test_graph_objective_can_adopt_its_first_matching_baseline(world):
    change(world.conn, "project", world.project["id"], workflow="graph")
    world.scheduler = Scheduler(world.service)
    run = world.scheduler.run(world.conn, world.actor, models.RunCreate(
        objective_id=world.document["id"], host_ids=[world.host["id"]],
        phase={"kind": "formalization"}, requested_phases=["formalization"]))
    manifest = save_blob(world.conn, world.service.store, world.project["id"], {"nodes": []})
    baseline = create(world.conn, "roadmap_snapshot", project_id=world.project["id"],
        roadmap_document_id=world.document["id"], source_commit_oid="a" * 40,
        graph_manifest_artifact_id=manifest["id"], acceptance_artifact_id=manifest["id"],
        status="frozen", frozen_at=datetime.now(timezone.utc))
    adopted = world.command("adopt_roadmap_snapshot", run, snapshot_id=str(baseline["id"]))
    assert adopted["adopted_roadmap_snapshot_id"] == baseline["id"]


def test_completed_objective_reopens_with_one_planner_successor(world):
    run = launch(world)
    planner = assignments(world, run)[0]
    change(world.conn, "assignment", planner["id"], status="cancelled", finished_at=func.now())
    change(world.conn, "mission", planner["mission_id"], status="cancelled",
        closed_at=func.now(), closure_note="Planning already accounted for")
    for obligation in world.ledger(planner["id"]):
        change(world.conn, "obligation", obligation["id"], status="done",
            resolution={"kind": "completed", "note": "Accounted for", "evidence": []})
    accepted = objectives.accept_phase(world.scheduler, world.conn, world.actor, run,
        {"note": "Accept the delivered objective", "evidence": [
            {"kind": "document", "id": str(world.document["id"])}]})
    objectives.advance(world.scheduler, world.conn, accepted, datetime.now(timezone.utc))
    completed = world.command("complete_run", get(world.conn, "run", run["id"]))
    reopened = world.command("reopen_run", completed, note="A new acceptance question needs work")
    assert reopened["status"] == "active"
    assert get(world.conn, "mission", run["mission_id"])["status"] == "open"
    pending = [row for row in assignments(world, reopened) if row["status"] == "pending"]
    assert len(pending) == 1 and pending[0]["recurrence_key"].endswith(":next")


@pytest.mark.parametrize("body", [None, True, 1, "text", ["username", "password"], [{}]])
def test_login_rejects_non_object_json_with_contract_error(api, body):
    client, *_ = api
    response = client.post("/api/v3/auth/login", content=json.dumps(body),
        headers={"Content-Type": "application/json"})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_credentials"


@pytest.mark.parametrize("body", [None, True, 1, ["epoch"], [{}]])
def test_worker_heartbeat_rejects_non_object_json_with_contract_error(api, body):
    client, _, _, _, _, token = api
    response = client.post(f"/api/v3/worker/executions/{uuid4()}/heartbeat",
        content=json.dumps(body),
        headers={**auth(token), "Content-Type": "application/json"})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_epoch"


def test_worker_can_request_maintenance_through_command_api(api):
    client, database, world, historical, _, _ = api
    with database.transaction() as conn:
        world.conn = conn
        world.disable_automations(historical)
        change(conn, "run", historical["id"], status="cancelled")
        run = launch(world)
        grant = world.claim()
        revision = get(conn, "run", run["id"])["revision"]
    body = {"operation": "request_maintenance", "target_id": str(run["id"]),
        "expected_revision": revision, "args": {"note": "Assess the phase evidence", "evidence": [
            {"kind": "document", "id": str(world.document["id"])}]}}
    response = mutate(client, "/api/v3/commands", body, grant["execution_token"])
    assert response.status_code == 200, response.text
    assert response.json()["result"]["role"] == "maintainer"
