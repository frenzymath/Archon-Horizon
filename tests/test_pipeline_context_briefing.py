import json
from datetime import datetime, timezone

from sqlalchemy import select

from archon_horizon.pipeline.execution.context_briefing import MAX_BYTES
from archon_horizon.pipeline.persistence.records import change, create, get, json_value
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_service import service_database, world


def test_default_context_is_bounded_without_losing_record_locators(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    grant = world.claim()
    change(world.conn, "mission", assignment["mission_id"], objective="Long mathematical target " * 10000)
    for number in range(2, 42):
        create(world.conn, "obligation", assignment_id=assignment["id"], number=number,
               kind="deliverable", description="Unfinished proof detail " * 5000)
    for _ in range(20):
        create(world.conn, "activity", assignment_id=assignment["id"], execution_id=grant["execution_id"],
               kind="progress", occurred_at=datetime.now(timezone.utc), summary="x" * 20000)
    brief = world.service.context(world.conn, world.actor, assignment["id"])
    assert len(json.dumps(json_value(brief), ensure_ascii=False, separators=(",", ":")).encode()) <= MAX_BYTES
    assert brief["activity"] == []
    assert brief["collections"]["obligations"]["selected"] == 41
    assert brief["collections"]["obligations"]["truncated"]
    assert "objective" in brief["mission"]["truncated_fields"]
    assert all(item["detail_url"] and item["revision"] for item in brief["obligations"])
    full = world.service.context(world.conn, world.actor, assignment["id"], view="full")
    assert len(full["obligations"]) == 41
    assert len(full["activity"]) == 20
    assert len(full["mission"]["objective"]) > 100000


def test_revision_condition_wakes_retained_owner_only_after_target_changes(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run, start_condition={"version": 1, "expression": {
        "op": "revision_after", "target": {"kind": "mission", "id": str(world.mission["id"])},
        "revision": world.mission["revision"]}})
    now = datetime.now(timezone.utc)
    assert not world.service.readiness(world.conn, assignment, now).ready
    change(world.conn, "mission", world.mission["id"], objective="An updated mathematical contract")
    assert world.service.readiness(world.conn, assignment, now).ready


def test_brief_context_includes_mission_contract_and_global_health(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    change(world.conn, "mission", assignment["mission_id"],
           acceptance_criteria=["The statement and all supporting definitions are checked"],
           delegation_note="Own this bounded contribution", max_open_children=3)
    brief = world.service.context(world.conn, world.actor, assignment["id"])
    assert brief["mission"]["acceptance_criteria"] == ["The statement and all supporting definitions are checked"]
    assert brief["mission"]["delegation_note"] == "Own this bounded contribution"
    assert brief["mission"]["max_open_children"] == 3
    assert "scope" in brief["mission"]
    assert brief["coordination"]["global"]["capacity"]["total_slots"] == 2


def test_successful_status_post_does_not_reset_no_progress_window(world):
    run = world.run(retry_policy={"max_no_progress_requests": 2})
    world.disable_automations(run)
    assignment = world.assignment(run)
    grant = world.claim()
    for number in (1, 2):
        create(world.conn, "provider_request", provider_thread_id=grant["provider_thread_record_id"],
               execution_id=grant["execution_id"], number=number, reason="continuation", status="completed")
    principal = world.conn.execute(select(tables["principal"]).where(
        tables["principal"].c.execution_id == grant["execution_id"])).mappings().one()
    operation = create(world.conn, "outbox_operation", actor_principal_id=principal["id"],
                       project_id=world.project["id"], kind="zulip_post", status="completed",
                       schema_version=1, idempotency_key="routine-status", payload={})
    assert world.service.progress_stalled(world.conn, assignment["id"], 2)
    change(world.conn, "outbox_operation", operation["id"], status="failed")
    assert not world.service.progress_stalled(world.conn, assignment["id"], 2)


def test_escaped_metadata_cannot_exceed_hard_context_budget(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    # JSON encodes these legal text characters using six bytes each.
    change(world.conn, "mission", assignment["mission_id"], title="\x01" * 10000, objective="\x02" * 10000)
    change(world.conn, "assignment", assignment["id"], instructions="\x03" * 10000)
    brief = world.service.context(world.conn, world.actor, assignment["id"])
    assert len(json.dumps(json_value(brief), ensure_ascii=False, separators=(",", ":")).encode()) <= MAX_BYTES
    assert "title" in brief["mission"]["truncated_fields"]
    assert "objective" in brief["mission"]["truncated_fields"]
    assert brief["mission"]["id"] == assignment["mission_id"]
    assert brief["mission"]["detail_url"].endswith(str(assignment["mission_id"]))
