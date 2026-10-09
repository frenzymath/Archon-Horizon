from datetime import datetime, timezone

from sqlalchemy import insert, select, update

from archon_horizon.pipeline.execution.coordination import usable_slots
from archon_horizon.pipeline.persistence.records import change, create, get
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_service import service_database, world  # noqa: F401
from test_pipeline_continuity import checkpoint_for_later, other_run_host


def planner(world, run):
    return dict(world.conn.execute(select(tables["assignment"]).where(
        tables["assignment"].c.run_id == run["id"],
        tables["assignment"].c.functions.contains(["planner"]))).mappings().one())


def ready(world, assignment):
    return world.service.readiness(world.conn, assignment, datetime.now(timezone.utc)).ready


def test_logically_ready_but_unplaceable_workers_do_not_suppress_planning(world):
    run = world.run()
    candidate = planner(world, run)
    unavailable = create(world.conn, "harness", slug="unavailable", adapter="codex_exec", adapter_version="1",
        provider_version="1", settings=world.harness["settings"], model_options={})
    for _ in range(2):
        worker = world.assignment(run, harness_id=unavailable["id"])
        assert ready(world, worker)
        assert world.scheduler.admission_blocker(world.conn, worker)
    assert ready(world, candidate)


def test_one_ready_worker_fills_one_shared_provider_slot(world):
    run = world.run()
    candidate = planner(world, run)
    world.assignment(run)
    limit = create(world.conn, "resource_limit", kind="provider_account", slug="one-provider-slot", max_concurrent=1)
    world.conn.execute(insert(tables["host_harness_limit"]).values(host_id=world.host["id"],
        harness_id=world.harness["id"], resource_limit_id=limit["id"]))
    assert not ready(world, candidate)


def test_planner_sleeps_after_empty_pass_until_new_terminal_work(world):
    run = world.run()
    candidate = planner(world, run)
    assert ready(world, candidate)
    change(world.conn, "automation", candidate["automation_id"], no_progress_count=1)
    assert not ready(world, candidate)
    worker = world.assignment(run)
    change(world.conn, "assignment", worker["id"], status="completed", finished_at=datetime.now(timezone.utc))
    assert ready(world, candidate)


def test_ready_worker_frontier_suppresses_redundant_planner(world):
    run = world.run()
    candidate = planner(world, run)
    for _ in range(2):
        world.assignment(run)
    assert not ready(world, candidate)


def test_capacity_preview_does_not_duplicate_shared_limits():
    state = {"shared_resources": [{"id": "provider", "remaining": 1}], "healthy_capacity_pools": [
        dict(free_slots=3, shared_limit_ids=["provider"], shared_resources_available=True),
        dict(free_slots=3, shared_limit_ids=["provider"], shared_resources_available=True),
        dict(free_slots=2, shared_limit_ids=[], shared_resources_available=True)]}
    assert usable_slots(state) == 3
    assert state["shared_resources"][0]["remaining"] == 1


def test_planning_does_not_bypass_recorded_admission_limit(world):
    run = world.run(max_assignments=1)
    candidate = planner(world, run)
    worker = world.assignment(run)
    change(world.conn, "assignment", worker["id"], status="completed", started_at=datetime.now(timezone.utc),
           finished_at=datetime.now(timezone.utc))
    assert not ready(world, candidate)


def test_retained_owners_on_one_host_do_not_fill_other_hosts_on_paper(world):
    run = world.run()
    candidate = planner(world, run)
    world.disable_automations(run)
    retained = []
    for _ in range(2):
        owner = world.assignment(run)
        grant = world.claim()
        checkpoint_for_later(world, owner, grant)
        retained.append(owner)
    for owner in retained:
        owner = change(world.conn, "assignment", owner["id"], not_before=None)
        assert ready(world, owner)
    slots = tables["host_harness"]
    world.conn.execute(update(slots).where(slots.c.host_id == world.host["id"]).values(execution_slots=1))
    other_run_host(world, run)
    change(world.conn, "automation", candidate["automation_id"], enabled=True)
    candidate = change(world.conn, "assignment", candidate["id"], status="pending")
    assert ready(world, candidate)
