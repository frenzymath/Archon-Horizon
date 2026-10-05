from datetime import timedelta

import pytest
from sqlalchemy import func, select

from archon_horizon.pipeline.conditions import Evaluation, Truth, evaluate
from archon_horizon.pipeline.records import change, create, get
from archon_horizon.pipeline.schema import tables
from test_pipeline_service import service_database, world


def publish(world, assignment):
    artifact = create(world.conn, "artifact", project_id=world.project["id"], kind="commit",
        content={"repository_id": str(world.workspace_repo["id"]), "commit_oid": "a" * 40})
    create(world.conn, "publication", artifact_id=artifact["id"], requested_by_assignment_id=assignment["id"],
        target={"ref_name": "refs/heads/bounded-deliverable"}, status="verified", verified_at=func.now())


@pytest.mark.parametrize("role", ["worker", "maintainer"])
@pytest.mark.parametrize("outcome", ["yielded", "succeeded"])
@pytest.mark.parametrize("planner_present", [False, True])
@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
def test_explicit_checkpoint_retains_ownership_until_dependency_settles(
        world, role, outcome, planner_present, terminal):
    run = world.run(retry_policy={"max_no_progress_requests": 2})
    world.disable_automations(run)
    closer = world.assignment(run, role=role)
    claim = world.claim()
    dependency = world.assignment(run)
    dependency_claim = world.claim()
    assert dependency_claim["assignment_id"] == str(dependency["id"])
    if role == "worker":
        publish(world, closer)
    now = world.conn.execute(select(func.now())).scalar_one()
    planner = (world.assignment(run, functions=["planner"], not_before=now + timedelta(days=1))
               if planner_present else None)
    for number in (1, 2):
        create(world.conn, "provider_request", provider_thread_id=claim["provider_thread_record_id"],
            execution_id=claim["execution_id"], number=number, reason="continuation", status="completed",
            created_at=now - timedelta(minutes=3 - number), finished_at=now)
    assert world.service.progress_stalled(world.conn, closer["id"], 2)
    condition = {"version": 1, "expression": {"op": "status_in",
        "target": {"kind": "assignment", "id": str(dependency["id"])},
        "values": ["completed", "failed", "cancelled"]}}
    checkpoint = world.command("checkpoint_assignment", get(world.conn, "assignment", closer["id"]),
        start_condition=condition, note="Retain root closure until the scoped integrator is terminal")

    settled = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", claim["execution_id"]), outcome)

    assert settled["status"] == "pending"
    assert settled["start_condition"] == checkpoint["start_condition"]
    assert settled["checkpoint_requested_at"] == checkpoint["checkpoint_requested_at"]
    assert settled["not_before"] is None
    assert world.ledger(closer["id"])[0]["status"] == "open"
    if planner:
        assert len(world.ledger(planner["id"])) == 1
    assert not world.service.readiness(world.conn, settled, now + timedelta(hours=1)).ready
    assert world.claim() is None

    world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", dependency_claim["execution_id"]), "cancelled")
    change(world.conn, "assignment", dependency["id"], status=terminal)
    resumed = world.claim()
    assert resumed["assignment_id"] == claim["assignment_id"]
    assert resumed["provider_thread_record_id"] == claim["provider_thread_record_id"]


@pytest.mark.parametrize("role", ["worker", "maintainer"])
def test_ordinary_bounded_completion_still_hands_unfinished_work_to_planner(world, role):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run, role=role)
    claim = world.claim()
    if role == "worker":
        publish(world, assignment)
    planner = world.assignment(run, functions=["planner"])

    settled = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", claim["execution_id"]), "succeeded")

    assert settled["status"] == "completed"
    assert world.ledger(assignment["id"])[0]["status"] == "superseded"
    assert len(world.ledger(planner["id"])) == 2


@pytest.mark.parametrize("role", ["worker", "maintainer"])
@pytest.mark.parametrize("wait", ["not_before", "after", "ready", "ready_and_timer", "unknown", "capacity"])
def test_explicit_checkpoint_preserves_no_progress_guard(world, role, wait):
    run = world.run(retry_policy={"max_no_progress_requests": 2})
    world.disable_automations(run)
    assignment = world.assignment(run, role=role)
    claim = world.claim()
    if role == "worker":
        publish(world, assignment)
    now = world.conn.execute(select(func.now())).scalar_one()
    for number in (1, 2):
        create(world.conn, "provider_request", provider_thread_id=claim["provider_thread_record_id"],
            execution_id=claim["execution_id"], number=number, reason="continuation", status="completed",
            created_at=now - timedelta(minutes=3 - number), finished_at=now)
    timer = {"op": "after", "at": (now + timedelta(hours=1)).isoformat()}
    ready = {"op": "status_in", "target": {"kind": "mission", "id": str(world.mission["id"])},
             "values": [get(world.conn, "mission", world.mission["id"])["status"]]}
    expressions = {"after": timer, "ready": ready,
        "ready_and_timer": {"op": "all", "args": [ready, timer]},
        "unknown": {"op": "forge_open_count", "project_id": str(world.project["id"]),
            "repository_ids": [str(world.workspace_repo["id"])], "kinds": ["pull_request"], "at_least": 1},
        "capacity": {"op": "queue_below", "run_id": str(run["id"]), "count": 0}}
    args = ({"not_before": timer["at"]} if wait == "not_before" else
            {"start_condition": {"version": 1, "expression": expressions[wait]}})
    world.command("checkpoint_assignment", get(world.conn, "assignment", assignment["id"]),
        **args, note="Same unfinished wait without new progress")

    settled = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", claim["execution_id"]), "succeeded")

    assert settled["status"] == "failed"
    assert "Continuation limit" in settled["status_note"]


@pytest.mark.parametrize("blocker", ["control", "failed_delivery", "journal", "budget"])
def test_blocked_event_checkpoint_does_not_bypass_settlement_or_execution_guards(world, blocker):
    run = world.run(retry_policy={"max_no_progress_requests": 2})
    world.disable_automations(run)
    assignment = world.assignment(run, role="maintainer")
    claim = world.claim()
    dependency = world.assignment(run)
    now = world.conn.execute(select(func.now())).scalar_one()
    for number in (1, 2):
        create(world.conn, "provider_request", provider_thread_id=claim["provider_thread_record_id"],
            execution_id=claim["execution_id"], number=number, reason="continuation", status="completed",
            created_at=now - timedelta(minutes=3 - number), finished_at=now)
    if blocker == "control":
        from archon_horizon.pipeline.notifications import OperatorNotice, operator_notice
        operator_notice(world.conn, world.actor, assignment["id"],
            OperatorNotice(message="Account for the failed publication before waiting"))
    elif blocker == "failed_delivery":
        principal = world.conn.execute(select(tables["principal"]).where(
            tables["principal"].c.execution_id == claim["execution_id"])).mappings().one()
        create(world.conn, "outbox_operation", actor_principal_id=principal["id"],
            project_id=world.project["id"], kind="forge_change", schema_version=1,
            idempotency_key="older-failed-change", payload={}, status="failed",
            updated_at=now - timedelta(minutes=5))
    condition = {"version": 1, "expression": {"op": "status_in",
        "target": {"kind": "assignment", "id": str(dependency["id"])},
        "values": ["completed", "failed", "cancelled"]}}
    world.command("checkpoint_assignment", get(world.conn, "assignment", assignment["id"]),
        start_condition=condition, note="Wait for dependency while other handling is outstanding")

    settled = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", claim["execution_id"]), "yielded",
        journal_pending=blocker == "journal",
        reason="execution_budget_reached" if blocker == "budget" else None)

    if blocker == "journal":
        assert settled["status"] == "pending"
        assert "intent reconciliation" in settled["status_note"]
    else:
        assert settled["status"] == "failed"
        assert ("Execution episode budget" if blocker == "budget" else "Continuation limit") in settled["status_note"]


@pytest.mark.parametrize("combination,expected", [
    ("all", Truth.FALSE), ("any", Truth.UNKNOWN), ("not", Truth.FALSE)])
def test_event_checkpoint_probe_respects_composite_logic(combination, expected):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    blocked = {"op": "status_in"}
    timer = {"op": "after", "at": (now + timedelta(hours=1)).isoformat()}
    expression = ({"op": "not", "arg": blocked} if combination == "not" else
                  {"op": combination, "args": [blocked, timer]})
    result = evaluate({"version": 1, "expression": expression}, now,
        lambda _: Evaluation(Truth.TRUE if combination == "not" else Truth.FALSE, "Event observation"),
        events_only=True)
    assert result.truth is expected
