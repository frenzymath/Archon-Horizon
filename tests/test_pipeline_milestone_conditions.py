from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select

from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.models import Condition, ObjectRef, RecordRef
from archon_horizon.pipeline.persistence.records import change, create, project_of
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_service import make_world, service_database, world


def terminal_condition(identifier):
    return {"version": 1, "expression": {"op": "status_in",
        "target": {"kind": "milestone_job", "id": str(identifier)},
        "values": ["completed", "failed"]}}


def job_for(world):
    workspace = world.conn.execute(select(tables["workspace"]).where(
        tables["workspace"].c.project_id == world.project["id"]).limit(1)).mappings().one()
    return create(world.conn, "milestone_job", project_id=world.project["id"], host_id=world.host["id"],
        workspace_id=workspace["id"], principal_id=world.actor.id,
        request={"workspace_id": str(workspace["id"]), "source_commit_oid": "a" * 40,
                 "base_commit_oid": "b" * 40})


def test_milestone_job_conditions_use_the_host_job_lifecycle():
    identifier = uuid4()
    condition = terminal_condition(identifier)
    assert RecordRef.model_validate(condition["expression"]["target"]).id == identifier
    assert Condition.model_validate(condition).expression.values == ["completed", "failed"]
    for invalid in ("cancelled", "verified", "succeeded", "pending"):
        condition["expression"]["values"] = [invalid]
        with pytest.raises(ValidationError, match="target lifecycle"):
            Condition.model_validate(condition)
    # Observing a job does not make it a persisted evidence/subscription selector.
    with pytest.raises(ValidationError):
        TypeAdapter(ObjectRef).validate_python({"kind": "milestone_job", "id": identifier})


@pytest.mark.parametrize("terminal", ["completed", "failed"])
def test_milestone_job_condition_wakes_only_after_build_settles(world, terminal):
    run = world.run()
    world.disable_automations(run)
    job = job_for(world)
    assignment = world.assignment(run, start_condition=terminal_condition(job["id"]))
    assert project_of(world.conn, "milestone_job", job["id"]) == world.project["id"]
    now = datetime.now(timezone.utc)
    for status in ("queued", "running"):
        change(world.conn, "milestone_job", job["id"], status=status)
        state = world.service.readiness(world.conn, assignment, now)
        assert not state.ready
        assert state.reason == f"milestone_job is {status}"
        assert world.claim() is None
    change(world.conn, "milestone_job", job["id"], status=terminal)
    assert world.service.readiness(world.conn, assignment, now).ready
    assert world.claim()["assignment_id"] == str(assignment["id"])


def test_milestone_job_conditions_reject_other_projects_and_missing_jobs(world, tmp_path):
    run = world.run()
    world.disable_automations(run)
    other = make_world(world.conn, tmp_path / "other")
    job = job_for(other)
    with pytest.raises(DomainError) as rejected:
        world.assignment(run, start_condition=terminal_condition(job["id"]))
    assert rejected.value.code == "scope_mismatch"
    with pytest.raises(DomainError) as missing:
        world.assignment(run, start_condition=terminal_condition(uuid4()))
    assert missing.value.code == "not_found"
