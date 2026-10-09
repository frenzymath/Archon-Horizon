"""Native delegation remains usable without taking primary-session slots."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, update

from archon_horizon.pipeline import models
from archon_horizon.pipeline.auth import authenticate
from archon_horizon.pipeline.dashboard.readmodels import resources
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.execution import objectives
from archon_horizon.pipeline.execution.coordination import summary
from archon_horizon.pipeline.persistence.records import change, create, get
from archon_horizon.pipeline.persistence.schema import tables
from archon_horizon.pipeline.projects.catalog import CatalogUpdate, update_catalog
from archon_horizon.pipeline.providers.provider_events import project_observation
from archon_horizon.pipeline.review.invocations import ReviewerPrepare, prepare
from test_pipeline_objectives import assignments, launch
from test_pipeline_service import service_database, world  # noqa: F401


def test_new_binding_defaults_to_uncapped_delegation():
    binding = models.HostHarnessCreate(host_id=uuid4(), harness_id=uuid4(),
        executable_path="/bin/codex", provider_home="/provider",
        credential_ref="secret:provider", execution_slots=1)
    assert binding.max_parallel_subagents is None
    assert binding.model_dump(mode="json")["max_parallel_subagents"] is None


def test_provider_guard_accepts_no_quota_but_build_pool_requires_one():
    guard = models.ResourceLimitCreate(kind="provider_account", slug="outage-guard", max_concurrent=None)
    assert guard.max_concurrent is None
    with pytest.raises(ValueError, match="build pools require"):
        models.ResourceLimitCreate(kind="build_pool", slug="compiler", max_concurrent=None)


def test_removing_provider_quota_preserves_its_outage_circuit(world):
    guard = create(world.conn, "resource_limit", kind="provider_account", slug="existing-guard",
                   max_concurrent=1, failure_count=2, circuit_open=True, circuit_reason="Diagnose provider outage")
    updated = update_catalog(world.conn, world.actor, "resource_limit", guard["id"],
        CatalogUpdate(expected_revision=guard["revision"], changes={"max_concurrent": None}))
    assert updated["max_concurrent"] is None
    assert updated["failure_count"] == 2 and updated["circuit_open"] is True
    assert updated["circuit_reason"] == guard["circuit_reason"]


def uncapped_binding(world):
    world.conn.execute(update(tables["host_harness"]).where(
        tables["host_harness"].c.host_id == world.host["id"]).values(
            max_parallel_subagents=None, execution_slots=1))


def test_one_slot_planner_retains_delegation_and_primary_capacity_accounting(world):
    uncapped_binding(world)
    run = launch(world)
    grant = world.claim()
    assert grant["max_parallel_subagents"] is None
    execution = get(world.conn, "execution", UUID(grant["execution_id"]))
    assert execution["native_capacity"] is None
    pool = next(item for item in summary(world.conn, world.service, run)["healthy_capacity_pools"]
                if item["host_id"] == world.host["id"])
    assert (pool["occupied"], pool["free_slots"]) == (1, 0)
    assert pool["shared_resources_available"] is True
    host = next(item for item in resources(world.conn, world.actor, world.service.config)["hosts"]
                if item["id"] == world.host["id"])
    assert host["occupied_slots"] == 1 and host["child_slots"] is None


@pytest.mark.parametrize("bounded", [False, True])
def test_observed_objective_children_are_accounted_once_and_release_on_stop(world, bounded):
    uncapped_binding(world)
    if bounded:
        world.conn.execute(update(tables["host_harness"]).values(
            max_parallel_subagents=1, execution_slots=4))
    limit = create(world.conn, "resource_limit", kind="provider_account",
                   slug="native-test-account", max_concurrent=10)
    world.conn.execute(insert(tables["host_harness_limit"]).values(
        host_id=world.host["id"], harness_id=world.harness["id"], resource_limit_id=limit["id"]))
    run = launch(world)
    if bounded:
        change(world.conn, "assignment", assignments(world, run)[0]["id"], status="cancelled")
        mission = objectives.child_mission(world.scheduler, world.conn, run,
            "Scoped proof", "Prove this independent lemma", ["Checked proof"])
        world.service.assignment(world.conn, world.actor, models.AssignmentCreate(
            run_id=run["id"], mission_id=mission["id"]))
    grant = world.claim()
    execution = get(world.conn, "execution", UUID(grant["execution_id"]))
    thread = get(world.conn, "provider_thread", UUID(grant["provider_thread_record_id"]))
    parent = create(world.conn, "provider_request", provider_thread_id=thread["id"],
                    execution_id=execution["id"], number=1, reason="assignment", status="running")

    def observe(raw):
        project_observation(world.conn, world.host_actor, execution, thread, parent["id"],
                            "codex", raw, uuid4(), datetime.now(timezone.utc), world.project["id"])

    def claimed():
        claim = tables["resource_claim"]
        return world.conn.execute(select(func.sum(claim.c.units)).where(
            claim.c.resource_limit_id == limit["id"], claim.c.released_at.is_(None))).scalar_one()

    spawn = {"type": "item.completed", "item": {"type": "collab_tool_call",
             "tool": "spawn_agent", "task_name": "scoped-helper", "receiver_thread_ids": ["native-child"]}}
    observe(spawn)
    observe(spawn)
    assert claimed() == 2
    observe({"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "wait",
             "agents_states": {"native-child": {"status": "completed"}}}})
    # Explicit caps reserve until the parent physically stops; uncapped sessions
    # release each observed child separately, preserving the parent’s claim.
    assert claimed() == (2 if bounded else 1)


def maintainer_context(world):
    run = launch(world)
    change(world.conn, "assignment", assignments(world, run)[0]["id"], status="cancelled")
    mission = objectives.child_mission(world.scheduler, world.conn, run,
        "Review the roadmap", "Assess the proposed statements", ["Review findings delivered"])
    assignment = world.service.assignment(world.conn, world.actor, models.AssignmentCreate(
        run_id=run["id"], mission_id=mission["id"], role="maintainer", category="maintenance"))
    grant = world.claim()
    assert grant["assignment_id"] == str(assignment["id"])
    actor = authenticate(world.conn, grant["execution_token"])
    thread = get(world.conn, "provider_thread", UUID(grant["provider_thread_record_id"]))
    parent = create(world.conn, "provider_request", provider_thread_id=thread["id"],
                    execution_id=UUID(grant["execution_id"]), number=1, reason="assignment", status="running")
    item = create(world.conn, "forge_item", repository_id=world.document["source_repository_id"],
        remote_number=1, kind="pull_request", origin_run_id=run["id"], review_phase="preprocessing",
        target_branch="main", title="Roadmap statements", status="open", head_commit_oid="a" * 40,
        observed_at=datetime.now(timezone.utc))
    return grant, actor, parent, item


def review_request(world, parent, item, number):
    descriptor = create(world.conn, "reviewer_descriptor", project_id=world.project["id"],
        slug=f"native-review-{number}", functions=["reviewer"], instructions="Assess statement meaning")
    return ReviewerPrepare(parent_request_id=parent["id"],
        forge_item_id=item["id"], reviewer_descriptor_id=descriptor["id"],
        expected_head_oid=item["head_commit_oid"], instructions="Assess the scoped question")


@pytest.mark.parametrize("provider_quota", [None, 4])
def test_uncapped_objective_can_prepare_multiple_reviewers_on_a_one_slot_host(world, provider_quota):
    uncapped_binding(world)
    if provider_quota is not None:
        limit = create(world.conn, "resource_limit", kind="provider_account",
                       slug="explicit-review-quota", max_concurrent=provider_quota)
        world.conn.execute(insert(tables["host_harness_limit"]).values(
            host_id=world.host["id"], harness_id=world.harness["id"], resource_limit_id=limit["id"]))
    grant, actor, parent, item = maintainer_context(world)
    assert grant["max_parallel_subagents"] is None
    # An admitted execution keeps its policy if the binding changes mid-session.
    world.conn.execute(update(tables["host_harness"]).values(max_parallel_subagents=0))
    for number in range(4):
        request = review_request(world, parent, item, number)
        if provider_quota is not None and number == 3:
            with pytest.raises(DomainError) as error:
                prepare(world.conn, actor, request, world.service)
            assert error.value.code == "review_capacity_unavailable"
            continue
        result = prepare(world.conn, actor, request, world.service)
        assert result["status"] == "pending"


def test_objective_finite_cap_remains_pinned_when_binding_becomes_uncapped(world):
    world.conn.execute(update(tables["host_harness"]).values(max_parallel_subagents=1, execution_slots=3))
    grant, actor, parent, item = maintainer_context(world)
    assert grant["max_parallel_subagents"] == 1
    world.conn.execute(update(tables["host_harness"]).values(max_parallel_subagents=None))
    result = prepare(world.conn, actor, review_request(world, parent, item, 0), world.service)
    assert result["status"] == "pending"
    with pytest.raises(DomainError) as error:
        prepare(world.conn, actor, review_request(world, parent, item, 1), world.service)
    assert error.value.code == "review_capacity_unavailable"
