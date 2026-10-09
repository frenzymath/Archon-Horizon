"""Agent-authored strategy must not inherit legacy milestone execution gates."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from test_pipeline_service import world, service_database
from archon_horizon.pipeline import models
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.execution import objectives
from archon_horizon.pipeline.execution.scheduler import Scheduler
from archon_horizon.pipeline.persistence.records import change, create, get
from archon_horizon.pipeline.persistence.schema import tables
from archon_horizon.pipeline.projects.catalog import CatalogUpdate, update_catalog
from archon_horizon.pipeline.review.decisions import postprocessing_review_panel
from archon_horizon.pipeline.roadmap_index import index_snapshot


def graph_project(world):
    return change(world.conn, "project", world.project["id"], workflow="graph")


def test_new_projects_default_to_agent_authored_graph_planning(world):
    project = world.service.project(world.conn, world.actor, models.ProjectCreate(
        slug="graph_" + world.project["slug"], title="Agent-led project"))
    assert project["workflow"] == "graph"


def test_graph_formalization_launch_needs_objective_but_no_milestone_baseline(world):
    graph_project(world)
    scheduler = Scheduler(world.service)
    run = scheduler.run(world.conn, world.actor, models.RunCreate(
        objective_id=world.document["id"], host_ids=[world.host["id"]],
        phase={"kind": "formalization"}, requested_phases=["formalization"]))
    assert run["phase"]["kind"] == "formalization"
    assert run["adopted_roadmap_snapshot_id"] is None
    assert world.conn.execute(select(tables["roadmap_snapshot"].c.id)).first() is None


def test_maintainer_acceptance_advances_graph_strategy_without_compiler_receipts(world):
    graph_project(world)
    world.scheduler = Scheduler(world.service)
    run = world.scheduler.run(world.conn, world.actor, models.RunCreate(
        objective_id=world.document["id"], host_ids=[world.host["id"]],
        requested_phases=["preprocessing", "formalization"]))
    planner = world.conn.execute(select(tables["assignment"]).where(
        tables["assignment"].c.run_id == run["id"])).mappings().one()
    world.scheduler.cancel(world.conn, world.actor, planner, "Strategy delivered")
    change(world.conn, "mission", planner["mission_id"], status="cancelled",
           closed_at=func.now(), closure_note="Strategy accounted for")
    accepted = world.command("accept_phase", get(world.conn, "run", run["id"]),
        note="Accept the reviewed objective and graph strategy",
        evidence=[{"kind": "document", "id": str(world.document["id"])}])
    assert accepted["pending_phase"]["kind"] == "formalization"
    objectives.reconcile(world.scheduler, world.conn, world.actor, datetime.now(timezone.utc))
    advanced = get(world.conn, "run", run["id"])
    assert advanced["phase"]["kind"] == "formalization"
    assert advanced["adopted_roadmap_snapshot_id"] is None


def test_graph_milestones_are_labels_and_uninterpreted_planning_metadata(world):
    graph_project(world)
    repo = world.document["source_repository_id"]
    index_snapshot(world.conn, world.actor, repo, "b" * 40, {
        "nodes/topology.md": "---\nlabel: topology\ntitle: Topology milestone\n"
            "labels: [milestone]\nmilestone:\n  strategy: Try a different decomposition\n"
            "---\n\nA human-readable route, with no required Lean locator.\n"})
    node = world.conn.execute(select(tables["node"]).where(
        tables["node"].c.source_path == "nodes/topology.md")).mappings().one()
    projection = world.conn.execute(select(tables["source_projection"]).where(
        tables["source_projection"].c.source_path == node["source_path"])).mappings().one()
    assert projection["metadata"]["labels"] == ["milestone"]
    assert projection["metadata"]["milestone"]["strategy"].startswith("Try")


def test_graph_roadmap_review_does_not_require_a_milestone_check(world):
    graph_project(world)
    policy = change(world.conn, "review_policy", world.policy["id"], specialist_mode="advisory")
    item = create(world.conn, "forge_item", repository_id=world.document["source_repository_id"],
        remote_number=91, kind="pull_request", title="Refine the strategy", status="open",
        head_commit_oid="b" * 40, target_branch="main", review_phase="preprocessing",
        observed_at=datetime.now(timezone.utc))
    panel = postprocessing_review_panel(world.conn, item, policy)
    assert "milestone-check" not in panel["missing"]


def test_operator_can_explicitly_transition_an_idle_milestone_project(world):
    project = change(world.conn, "project", world.project["id"], workflow="milestones")
    switched = update_catalog(world.conn, world.actor, "project", project["id"],
        CatalogUpdate(expected_revision=project["revision"], changes={"workflow": "graph"}))
    assert switched["workflow"] == "graph"
    world.run()
    with pytest.raises(DomainError) as error:
        update_catalog(world.conn, world.actor, "project", switched["id"],
            CatalogUpdate(expected_revision=switched["revision"], changes={"workflow": "milestones"}))
    assert error.value.code == "active_project_runs"


def test_new_orchestrator_launch_is_rejected_while_legacy_fixtures_remain_readable(world):
    with pytest.raises(DomainError) as error:
        Scheduler(world.service).run(world.conn, world.actor, models.RunCreate(
            mission_id=world.mission["id"], orchestration="legacy", host_ids=[world.host["id"]],
            phase={"kind": "preprocessing", "roadmap_document_id": world.document["id"], "orchestrated": True}))
    assert error.value.code == "retired_orchestrator"
    saved = world.run(orchestrated=True)
    assert saved["phase"]["orchestrated"] is True
