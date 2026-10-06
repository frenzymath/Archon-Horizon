from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from archon_horizon.pipeline.conditions import Truth
from archon_horizon.pipeline.records import change, create, get, object_ref
from archon_horizon.pipeline.refill import recheck
from archon_horizon.pipeline.schema import tables
from test_pipeline_service import service_database, world


def waiting_planner(world):
    run = world.run()
    a = tables["assignment"]
    planner = world.conn.execute(select(a).where(a.c.run_id == run["id"],
        a.c.functions.contains(["planner"]))).mappings().one()
    condition = {"version": 1, "expression": {"op": "queue_below",
        "run_id": str(run["id"]), "count": 0}}
    rule = get(world.conn, "automation", planner["automation_id"])
    world.command("defer_automation", rule, start_condition=condition)
    return run, get(world.conn, "assignment", planner["id"]), condition


@pytest.mark.parametrize("observation", ["false", "unknown"])
@pytest.mark.parametrize("combination", ["direct", "all", "any", "not"])
def test_idle_capacity_preserves_explicit_external_wait(world, observation, combination):
    run, planner, _ = waiting_planner(world)
    owner = world.assignment(run)
    assert world.claim()["assignment_id"] == str(owner["id"])
    if observation == "false":
        expression = {"op": "status_in", "target": {"kind": "assignment", "id": str(owner["id"])},
                      "values": ["running"] if combination == "not" else ["completed", "failed", "cancelled"]}
    else:
        expression = {"op": "forge_open_count", "project_id": str(world.project["id"]),
                      "repository_ids": [str(world.workspace_repo["id"])],
                      "kinds": ["pull_request"], "at_least": 1}
    if combination in ("all", "any"):
        expression = {"op": combination, "args": [expression, {"op": "queue_below",
            "run_id": str(run["id"]), "count": 1 if combination == "all" else 0}]}
    elif combination == "not":
        expression = {"op": "not", "arg": expression}
    rule = get(world.conn, "automation", planner["automation_id"])
    condition = {"version": 1, "expression": expression}
    rule = world.command("defer_automation", rule, start_condition=condition, no_progress=False)
    condition = rule["start_condition"]
    planner = get(world.conn, "assignment", planner["id"])
    now = world.conn.execute(select(func.now())).scalar_one() + timedelta(minutes=6)
    assert world.service.readiness(world.conn, planner, now).truth is (
        Truth.FALSE if observation == "false" else Truth.UNKNOWN)
    before = world.conn.execute(select(func.count()).select_from(tables["assignment"])).scalar_one()
    assert recheck(world.scheduler, world.conn, world.actor, now) == 0
    assert recheck(world.scheduler, world.conn, world.actor, now + timedelta(days=1)) == 0
    assert get(world.conn, "assignment", planner["id"])["start_condition"] == condition
    assert get(world.conn, "automation", rule["id"])["start_condition"] == condition
    assert world.conn.execute(select(func.count()).select_from(tables["assignment"])).scalar_one() == before
    assert not any(row["description"].startswith("Idle queue recheck:") for row in world.ledger(planner["id"]))


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
def test_explicit_owner_wait_resumes_on_owner_terminal_transition(world, terminal):
    run, planner, _ = waiting_planner(world)
    owner = world.assignment(run)
    assert world.claim()["assignment_id"] == str(owner["id"])
    rule = get(world.conn, "automation", planner["automation_id"])
    condition = {"version": 1, "expression": {"op": "status_in",
        "target": {"kind": "assignment", "id": str(owner["id"])}, "values": ["completed", "failed", "cancelled"]}}
    rule = world.command("defer_automation", rule, start_condition=condition)
    planner = get(world.conn, "assignment", planner["id"])
    now = world.conn.execute(select(func.now())).scalar_one() + timedelta(minutes=6)
    assert not world.service.readiness(world.conn, planner, now).ready
    assert recheck(world.scheduler, world.conn, world.actor, now) == 0
    change(world.conn, "assignment", owner["id"], status=terminal, finished_at=now)
    assert world.service.readiness(world.conn, planner, now).ready
    assert world.claim()["assignment_id"] == str(planner["id"])
    assert get(world.conn, "assignment", planner["id"])["start_condition"] == rule["start_condition"]


def test_deferring_unchanged_capacity_wait_does_not_reset_reconsideration(world):
    _, planner, condition = waiting_planner(world)
    now = world.conn.execute(select(func.now())).scalar_one() + timedelta(minutes=6)
    assert recheck(world.scheduler, world.conn, world.actor, now) == 1
    rule = get(world.conn, "automation", planner["automation_id"])
    rule = world.command("defer_automation", rule, start_condition=condition)
    assert rule["no_progress_count"] == 1
    assert recheck(world.scheduler, world.conn, world.actor, now + timedelta(days=1)) == 0


def test_capacity_wait_can_reconsider_logically_ready_but_unplaceable_work(world):
    run, planner, _ = waiting_planner(world)
    unavailable = create(world.conn, "harness", slug="unavailable", adapter="codex_exec", adapter_version="1",
        provider_version="1", settings=world.harness["settings"], model_options={})
    worker = world.assignment(run, harness_id=unavailable["id"])
    condition = {"version": 1, "expression": {"op": "queue_below", "run_id": str(run["id"]), "count": 1}}
    world.command("defer_automation", get(world.conn, "automation", planner["automation_id"]), start_condition=condition)
    now = world.conn.execute(select(func.now())).scalar_one() + timedelta(minutes=6)
    assert world.service.readiness(world.conn, worker, now).ready
    assert world.scheduler.admission_blocker(world.conn, worker)
    assert recheck(world.scheduler, world.conn, world.actor, now) == 1
    assert get(world.conn, "assignment", planner["id"])["start_condition"] is None


def test_planning_needed_reconsiders_a_new_unplaceable_frontier(world):
    run = world.run()
    assignment = tables["assignment"]
    planner = world.conn.execute(select(assignment).where(assignment.c.run_id == run["id"],
        assignment.c.functions.contains(["planner"]))).mappings().one()
    assert planner["start_condition"]["expression"]["op"] == "planning_needed"
    change(world.conn, "automation", planner["automation_id"], no_progress_count=1)
    unavailable = create(world.conn, "harness", slug="unavailable", adapter="codex_exec", adapter_version="1",
        provider_version="1", settings=world.harness["settings"], model_options={})
    now = world.conn.execute(select(func.now())).scalar_one() + timedelta(minutes=6)
    create(world.conn, "assignment", run_id=run["id"], mission_id=world.mission["id"],
        number=3, queue_rank=3000, harness_id=unavailable["id"], created_at=now - timedelta(minutes=5))
    assert not world.service.readiness(world.conn, planner, now).ready
    assert recheck(world.scheduler, world.conn, world.actor, now) == 1
    assert get(world.conn, "assignment", planner["id"])["start_condition"] is None


def test_partial_occupancy_rechecks_same_planner_without_duplicating_work(world):
    run, planner, condition = waiting_planner(world)
    worker = world.assignment(run)
    assert world.claim()["assignment_id"] == str(worker["id"])
    world.assignment(run, not_before=world.conn.execute(select(func.now())).scalar_one() + timedelta(days=1))
    before = world.conn.execute(select(func.count()).select_from(tables["assignment"])).scalar_one()
    now = world.conn.execute(select(func.now())).scalar_one() + timedelta(minutes=6)
    assert recheck(world.scheduler, world.conn, world.actor, now) == 1
    assert get(world.conn, "assignment", worker["id"])["status"] == "running"
    assert get(world.conn, "assignment", planner["id"])["start_condition"] is None
    assert get(world.conn, "automation", planner["automation_id"])["start_condition"] == condition
    assert world.conn.execute(select(func.count()).select_from(tables["assignment"])).scalar_one() == before
    change(world.conn, "assignment", planner["id"], start_condition=condition)
    assert recheck(world.scheduler, world.conn, world.actor, now + timedelta(days=2)) == 0


@pytest.mark.parametrize("guard", ["not_before", "after", "retry", "budget", "ready_work", "full_capacity"])
def test_refill_respects_time_budget_and_existing_work(world, guard):
    run, planner, condition = waiting_planner(world)
    now = world.conn.execute(select(func.now())).scalar_one() + timedelta(minutes=6)
    if guard == "not_before":
        change(world.conn, "assignment", planner["id"], not_before=now + timedelta(hours=1))
    elif guard == "after":
        change(world.conn, "assignment", planner["id"], start_condition={"version": 1, "expression": {
            "op": "all", "args": [condition["expression"], {"op": "after", "at": (now + timedelta(hours=1)).isoformat()}]}})
    elif guard == "retry":
        change(world.conn, "assignment", planner["id"], retry_at=now + timedelta(hours=1))
    elif guard == "budget":
        change(world.conn, "run", run["id"], token_budget=0)
    elif guard == "ready_work":
        world.assignment(run)
    else:
        world.assignment(run)
        world.assignment(run)
        assert world.claim() and world.claim()
    assert recheck(world.scheduler, world.conn, world.actor, now) == 0


def test_substantive_progress_allows_another_bounded_recheck(world):
    run, planner, condition = waiting_planner(world)
    now = world.conn.execute(select(func.now())).scalar_one()
    assert recheck(world.scheduler, world.conn, world.actor, now + timedelta(minutes=6)) == 1
    change(world.conn, "assignment", planner["id"], start_condition=condition)
    worker = world.assignment(run)
    change(world.conn, "assignment", worker["id"], status="completed", finished_at=now + timedelta(minutes=7))
    assert recheck(world.scheduler, world.conn, world.actor, now + timedelta(minutes=13)) == 1


def test_queue_below_ignores_sleeping_work_and_recurring_sessions(world):
    run = world.run()
    now = world.conn.execute(select(func.now())).scalar_one()
    a = tables["assignment"]
    planner = world.conn.execute(select(a).where(a.c.run_id == run["id"],
        a.c.functions.contains(["planner"]))).mappings().one()
    for _ in range(4):
        world.assignment(run, not_before=now + timedelta(days=1))
    assert world.service.readiness(world.conn, planner, now).ready
    for _ in range(2):
        world.assignment(run)
    assert not world.service.readiness(world.conn, planner, now).ready


@pytest.mark.parametrize("result", ["publication", "merge"])
def test_persistent_worker_result_permits_one_new_refill_recheck(world, result):
    run, planner, condition = waiting_planner(world)
    now = world.conn.execute(select(func.now())).scalar_one()
    worker = world.assignment(run)
    assert world.claim()["assignment_id"] == str(worker["id"])
    assert recheck(world.scheduler, world.conn, world.actor, now + timedelta(minutes=6)) == 1
    change(world.conn, "assignment", planner["id"], start_condition=condition)
    result_time = world.conn.execute(select(func.clock_timestamp())).scalar_one()
    if result == "publication":
        artifact = create(world.conn, "artifact", project_id=world.project["id"], kind="commit",
                          content={"repository_id": str(world.workspace_repo["id"]), "commit_oid": "a" * 40})
        create(world.conn, "publication", artifact_id=artifact["id"], requested_by_assignment_id=worker["id"],
               target={"ref_name": "refs/heads/cluster-port"}, status="verified", verified_at=result_time)
    else:
        item = create(world.conn, "forge_item", repository_id=world.workspace_repo["id"],
                      origin_run_id=run["id"], kind="pull_request", remote_number=1,
                      title="Cluster result", status="merged", observed_at=result_time)
        frontier_event(world, item, ["status"], result_time)
    assert recheck(world.scheduler, world.conn, world.actor, result_time + timedelta(minutes=1)) == 0
    assert recheck(world.scheduler, world.conn, world.actor, result_time + timedelta(minutes=6)) == 1
    assert get(world.conn, "assignment", worker["id"])["status"] == "running"
    change(world.conn, "assignment", planner["id"], start_condition=condition)
    assert recheck(world.scheduler, world.conn, world.actor, result_time + timedelta(minutes=12)) == 0


def frontier_event(world, item, fields, at):
    return create(world.conn, "event", project_id=world.project["id"],
                  subject_id=object_ref(world.conn, "forge_item", item["id"]), actor_principal_id=world.actor.id,
                  kind="record_changed", schema_version=1, source="control_plane", source_event_id=str(uuid4()),
                  created_at=at, occurred_at=at, payload={"revision": item["revision"], "changed_fields": fields})


@pytest.mark.parametrize("noise", ["poll", "label", "another_run", "planner_publication"])
def test_refill_ignores_observation_noise_and_unrelated_results(world, noise):
    run, planner, condition = waiting_planner(world)
    now = world.conn.execute(select(func.now())).scalar_one()
    change(world.conn, "automation", planner["automation_id"], no_progress_count=1)
    later = now + timedelta(minutes=10)
    if noise == "planner_publication":
        artifact = create(world.conn, "artifact", project_id=world.project["id"], kind="commit", content={})
        create(world.conn, "publication", artifact_id=artifact["id"], requested_by_assignment_id=planner["id"],
               target={"ref_name": "refs/heads/planning-notes"}, status="verified", verified_at=later)
    else:
        other = world.run() if noise == "another_run" else run
        if noise == "another_run":
            world.disable_automations(other)
        item = create(world.conn, "forge_item", repository_id=world.workspace_repo["id"], origin_run_id=other["id"],
                      kind="pull_request", remote_number=1, title="Observed result", status="merged", observed_at=later)
        frontier_event(world, item,
                       ["status"] if noise == "another_run" else ["labels"] if noise == "label" else ["observed_at"], later)
    assert recheck(world.scheduler, world.conn, world.actor, now + timedelta(minutes=20)) == 0
    assert get(world.conn, "assignment", planner["id"])["start_condition"] == condition
