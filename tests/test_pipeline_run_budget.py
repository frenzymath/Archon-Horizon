from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.records import change, get
from archon_horizon.pipeline.schema import tables
from test_pipeline_service import service_database, world


def test_extend_exhausted_run_admits_work_without_restarting_existing_context(world):
    run = world.run(max_assignments=1)
    world.disable_automations(run)
    active = world.assignment(run)
    claim = world.claim()
    pending = world.assignment(run)
    now = datetime.now(timezone.utc)
    assert not world.service.readiness(world.conn, pending, now).ready
    updated = world.command("extend_run_budget", run, additional_assignments=100,
                            note="Operator authorized another 100 sessions")
    assert updated["max_assignments"] == 101
    assert updated["status"] == "active"
    assert world.service.readiness(world.conn, pending, now).ready
    assert get(world.conn, "assignment", active["id"])["status"] == "running"
    assert get(world.conn, "provider_thread", claim["provider_thread_record_id"])["assignment_id"] == active["id"]
    with pytest.raises(DomainError, match="record changed"):
        world.command("extend_run_budget", run, additional_assignments=100, note="Duplicate stale request")
    assert get(world.conn, "run", run["id"])["max_assignments"] == 101


def test_budget_extension_requires_installation_operator(world):
    run = world.run(max_assignments=100)
    world.conn.execute(delete(tables["system_grant"]).where(
        tables["system_grant"].c.principal_id == world.actor.id))
    with pytest.raises(DomainError) as error:
        world.command("extend_run_budget", run, additional_assignments=100, note="Unauthorized increase")
    assert error.value.code == "forbidden"
    assert get(world.conn, "run", run["id"])["max_assignments"] == 100


@pytest.mark.parametrize("amount", [0, -1, True, "100", 1.5])
def test_budget_extension_rejects_invalid_increment(world, amount):
    run = world.run(max_assignments=100)
    with pytest.raises(DomainError) as error:
        world.command("extend_run_budget", run, additional_assignments=amount, note="Invalid increment")
    assert error.value.code == "invalid_arguments"


def test_budget_extension_preserves_pause_and_other_limits(world):
    run = world.run(max_assignments=100, token_budget=10000)
    paused = world.command("pause_run", run)
    extended = world.command("extend_run_budget", paused, additional_assignments=100, note="Next batch")
    assert extended["status"] == "paused"
    assert extended["token_budget"] == 10000
    assert extended["max_assignments"] == 200


def test_budget_extension_rejects_unlimited_and_settled_runs(world):
    unlimited = world.run()
    with pytest.raises(DomainError) as error:
        world.command("extend_run_budget", unlimited, additional_assignments=100, note="Next batch")
    assert error.value.code == "unlimited_run"
    limited = world.run(max_assignments=100)
    settled = change(world.conn, "run", limited["id"], status="completed")
    with pytest.raises(DomainError) as error:
        world.command("extend_run_budget", settled, additional_assignments=100, note="Next batch")
    assert error.value.code == "invalid_transition"


def test_operator_can_remove_assignment_cap_preserving_context_and_other_limits(world):
    run = world.run(max_assignments=1, token_budget=10000)
    world.disable_automations(run)
    active = world.assignment(run)
    claim = world.claim()
    pending = world.assignment(run)
    assert not world.service.readiness(world.conn, pending, datetime.now(timezone.utc)).ready
    updated = world.command("set_run_budget", run, max_assignments=None,
                            note="Continue this project without an arbitrary assignment count cap")
    assert updated["max_assignments"] is None
    assert updated["token_budget"] == 10000
    assert world.service.readiness(world.conn, pending, datetime.now(timezone.utc)).ready
    assert get(world.conn, "assignment", active["id"])["status"] == "running"
    assert get(world.conn, "provider_thread", claim["provider_thread_record_id"])["assignment_id"] == active["id"]
    with pytest.raises(DomainError, match="record changed"):
        world.command("set_run_budget", run, max_assignments=20, note="Stale edit")


def test_changing_assignment_cap_requires_operator_and_explanation(world):
    run = world.run(max_assignments=1)
    with pytest.raises(DomainError) as error:
        world.command("set_run_budget", run, max_assignments=None, note=" ")
    assert error.value.code == "invalid_arguments"
    world.conn.execute(delete(tables["system_grant"]).where(
        tables["system_grant"].c.principal_id == world.actor.id))
    with pytest.raises(DomainError) as error:
        world.command("set_run_budget", run, max_assignments=None, note="Unapproved budget change")
    assert error.value.code == "forbidden"
