from datetime import timedelta

import pytest
from sqlalchemy import func, select

from archon_horizon.pipeline.execution.admission import reason, run_usage
from archon_horizon.pipeline.execution.coordination import summary
from archon_horizon.pipeline.persistence.records import change, get
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_service import service_database, world


def finish(world, grant):
    for obligation in world.ledger(grant["assignment_id"]):
        change(world.conn, "obligation", obligation["id"], status="done",
               resolution={"kind": "completed", "note": "Bounded task delivered", "evidence": []})
    return world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", grant["execution_id"]), "succeeded")


def test_supervision_neither_consumes_nor_is_blocked_by_productive_admission_budget(world):
    run = world.run(max_assignments=1, orchestrated=True)
    supervisor = world.claim()
    assert get(world.conn, "assignment", supervisor["assignment_id"])["functions"] == ["orchestrator"]
    finish(world, supervisor)
    worker = world.assignment(run)
    assert world.claim()["assignment_id"] == str(worker["id"])
    successor = dict(world.conn.execute(select(tables["assignment"]).where(
        tables["assignment"].c.run_id == run["id"], tables["assignment"].c.status == "pending",
        tables["assignment"].c.functions.contains(["orchestrator"]))).mappings().one())
    change(world.conn, "assignment", successor["id"], not_before=None)
    next_worker = world.assignment(run)
    now = world.conn.execute(select(func.now())).scalar_one()
    assert not world.service.readiness(world.conn, next_worker, now).ready
    assert world.claim()["assignment_id"] == str(successor["id"])
    usage = run_usage(world.conn, run)
    assert usage["admitted_assignments"] == 1
    assert usage["supervision_assignments"] == 2
    observed = summary(world.conn, world.service, run)["admission"]
    assert observed["remaining_new_assignments"] == 0
    assert observed["supervision_blocker"] is None


@pytest.mark.parametrize("guard", ["tokens", "deadline", "uncertain_usage"])
def test_supervision_preserves_explicit_total_budget_guards(world, guard):
    run = world.run(max_assignments=1, orchestrated=True)
    now = world.conn.execute(select(func.now())).scalar_one()
    usage = {"admitted_assignments": 1, "observed_tokens": 1, "usage_uncertain": False}
    if guard == "tokens":
        run = {**run, "token_budget": 1}
    elif guard == "deadline":
        run = {**run, "expires_at": now - timedelta(seconds=1)}
    else:
        run = {**run, "token_budget": 100}
        usage["usage_uncertain"] = True
    assert reason(run, usage, now, supervisory=True) is not None


def test_due_supervisor_gets_next_slot_without_overbooking_running_workers(world):
    run = world.run(orchestrated=True)
    world.disable_automations(run)
    world.assignment(run)
    world.assignment(run)
    first, second = world.claim(), world.claim()
    queued_worker = world.assignment(run)
    supervisor = world.assignment(run, role="maintainer", functions=["orchestrator"])
    assert queued_worker["queue_rank"] < supervisor["queue_rank"]
    assert world.claim() is None
    finish(world, first)
    control = world.claim()
    assert control["assignment_id"] == str(supervisor["id"])
    assert get(world.conn, "execution", second["execution_id"])["status"] in ("starting", "running")
    world.assignment(run, role="maintainer", functions=["orchestrator"])
    finish(world, second)
    assert world.claim()["assignment_id"] == str(queued_worker["id"])


def queued(world, run, supervisory, *, ready):
    return world.assignment(run, **({"role": "maintainer", "functions": ["orchestrator"]} if supervisory else {}),
        start_condition={"version": 1, "expression": {"op": "status_in",
            "target": {"kind": "mission", "id": str(world.mission["id"])},
            "values": ["open" if ready else "completed"]}})


@pytest.mark.parametrize("supervisory", [True, False])
def test_blocked_profile_page_does_not_hide_other_profile(world, monkeypatch, supervisory):
    monkeypatch.setattr(world.scheduler, "CLAIM_SCAN_LIMIT", 2)
    run = world.run(orchestrated=True)
    world.disable_automations(run)
    if not supervisory:
        world.assignment(run, role="maintainer", functions=["orchestrator"])
        finish(world, world.claim())
    for _ in range(3):
        queued(world, run, supervisory, ready=False)
    ready = queued(world, run, not supervisory, ready=True)
    assert world.claim()["assignment_id"] == str(ready["id"])


@pytest.mark.parametrize("supervisory", [True, False])
def test_rejected_pages_rotate_and_success_resets_queue_priority(world, monkeypatch, supervisory):
    monkeypatch.setattr(world.scheduler, "CLAIM_SCAN_LIMIT", 2)
    run = world.run(orchestrated=supervisory)
    world.disable_automations(run)
    blocked = [queued(world, run, supervisory, ready=False) for _ in range(3)]
    ready = queued(world, run, supervisory, ready=True)
    assert world.claim() is None
    admitted = world.claim()
    assert admitted["assignment_id"] == str(ready["id"])
    finish(world, admitted)
    change(world.conn, "assignment", blocked[0]["id"], start_condition=ready["start_condition"])
    assert world.claim()["assignment_id"] == str(blocked[0]["id"])


def test_scan_wraps_after_exhausted_page(world, monkeypatch):
    monkeypatch.setattr(world.scheduler, "CLAIM_SCAN_LIMIT", 2)
    run = world.run()
    world.disable_automations(run)
    blocked = [queued(world, run, False, ready=False) for _ in range(3)]
    assert world.claim() is None
    assert world.claim() is None
    change(world.conn, "assignment", blocked[0]["id"], start_condition=None)
    assert world.claim()["assignment_id"] == str(blocked[0]["id"])
