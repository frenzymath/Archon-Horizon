from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, update

from archon_horizon.pipeline import models
from archon_horizon.pipeline.auth import Actor
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.records import change, create, get
from archon_horizon.pipeline.schema import tables
from archon_horizon.pipeline.worker_events import WorkerOperation, handle as worker_event
from test_pipeline_service import service_database, world


@pytest.mark.parametrize("role", ["worker", "maintainer"])
@pytest.mark.parametrize("prior_wait", [False, True])
def test_delivered_task_finishes_while_pr_remains_open(world, role, prior_wait):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run, role=role)
    claim = world.claim()
    create(world.conn, "provider_request", provider_thread_id=claim["provider_thread_record_id"],
           execution_id=claim["execution_id"], number=1, reason="assignment", status="completed",
           finished_at=datetime.now(timezone.utc))
    pr = create(world.conn, "forge_item", repository_id=world.workspace_repo["id"], remote_number=1,
                kind="pull_request", title="Published contribution awaiting follow-up", status="open",
                labels=["awaiting-review"], observed_at=func.now())
    root = world.ledger(assignment["id"])[0]
    if prior_wait:
        world.command("checkpoint_assignment", get(world.conn, "assignment", assignment["id"]),
                      note="Old request to wait for PR feedback", not_before=(
                          datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    world.service.resolve_obligation(world.conn, world.actor, root["id"], models.ObligationResolve(
        expected_revision=root["revision"], status="done", resolution={"kind": "completed",
            "note": "Published the scoped deliverable; maintenance handles subsequent PR decisions",
            "evidence": [{"kind": "forge_item", "id": pr["id"]}]}))
    heartbeat = world.scheduler.heartbeat(world.conn, world.host_actor, claim["execution_id"], claim["epoch"])
    assert not heartbeat["yield"] and not heartbeat["continue"]
    settled = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", claim["execution_id"]), "succeeded")
    assert settled["status"] == "completed"
    assert settled["checkpoint_requested_at"] is None
    assert settled["not_before"] is None
    assert get(world.conn, "forge_item", pr["id"])["status"] == "open"
    assert get(world.conn, "run", run["id"])["status"] == "active"


def test_checkpoint_releases_slot_and_resumes_same_native_context(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    thread = change(world.conn, "provider_thread", claim["provider_thread_record_id"],
                    provider_thread_id="retained-context", status="available")
    create(world.conn, "provider_request", provider_thread_id=thread["id"], execution_id=claim["execution_id"],
           number=1, reason="assignment", status="completed", finished_at=datetime.now(timezone.utc))
    until = datetime.now(timezone.utc) + timedelta(hours=1)
    running = get(world.conn, "assignment", assignment["id"])
    world.command("checkpoint_assignment", running, not_before=until.isoformat(), note="Wait for an external result")
    heartbeat = world.scheduler.heartbeat(world.conn, world.host_actor, claim["execution_id"], claim["epoch"])
    assert heartbeat["yield"] and not heartbeat["continue"]
    settled = world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", claim["execution_id"]), "yielded")
    assert settled["status"] == "pending" and settled["not_before"] == until
    assert world.claim() is None


def test_failed_provider_turn_consumes_no_progress_window(world):
    run = world.run(retry_policy={"max_no_progress_requests": 3})
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    now = datetime.now(timezone.utc)
    for number, status in ((1, "failed"), (2, "completed"), (3, "completed")):
        create(world.conn, "provider_request", provider_thread_id=claim["provider_thread_record_id"],
               execution_id=claim["execution_id"], number=number, reason="continuation", status=status,
               created_at=now - timedelta(minutes=4 - number), finished_at=now - timedelta(minutes=4 - number))
    assert world.service.progress_stalled(world.conn, assignment["id"], 3)
    assert world.ledger(assignment["id"])[0]["status"] == "open"
    world.command("update_assignment", settled, not_before=None)
    resumed = world.claim()
    assert resumed["assignment_id"] == claim["assignment_id"]
    assert resumed["provider_thread_record_id"] == claim["provider_thread_record_id"]
    assert resumed["provider_thread_id"] == "retained-context"
    assert get(world.conn, "assignment", assignment["id"])["checkpoint_requested_at"] is None


def test_checkpoint_releases_slot_while_outbox_delivers_and_resumes_context(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    change(world.conn, "provider_thread", claim["provider_thread_record_id"],
           provider_thread_id="delivery-wait-context", status="available")
    create(world.conn, "provider_request", provider_thread_id=claim["provider_thread_record_id"],
           execution_id=claim["execution_id"], number=1, reason="assignment", status="completed",
           finished_at=datetime.now(timezone.utc))
    principal = world.conn.execute(select(tables["principal"]).where(
        tables["principal"].c.execution_id == claim["execution_id"])).mappings().one()
    delivery = create(world.conn, "outbox_operation", actor_principal_id=principal["id"], project_id=world.project["id"],
                      kind="forge_comment", schema_version=1, idempotency_key="outstanding", payload={})
    world.command("checkpoint_assignment", get(world.conn, "assignment", assignment["id"]), note="Wait for review")
    heartbeat = world.scheduler.heartbeat(world.conn, world.host_actor, claim["execution_id"], claim["epoch"])
    assert heartbeat["yield"] and not heartbeat["continue"]
    settled = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", claim["execution_id"]), "yielded")
    assert settled["status"] == "pending"
    assert world.claim() is None
    change(world.conn, "outbox_operation", delivery["id"], status="completed")
    resumed = world.claim()
    assert resumed["assignment_id"] == str(assignment["id"])
    assert resumed["provider_thread_id"] == "delivery-wait-context"


@pytest.mark.parametrize("delivery_status", ["pending", "running", "uncertain"])
@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
def test_delivery_wait_does_not_fail_no_progress_and_terminal_result_wakes(world, delivery_status, terminal):
    run = world.run(retry_policy={"max_no_progress_requests": 2})
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    now = world.conn.execute(select(func.now())).scalar_one()
    for number in (1, 2):
        create(world.conn, "provider_request", provider_thread_id=claim["provider_thread_record_id"],
            execution_id=claim["execution_id"], number=number, reason="continuation", status="completed",
            created_at=now-timedelta(minutes=3-number), finished_at=now-timedelta(minutes=3-number))
    principal = world.conn.execute(select(tables["principal"]).where(
        tables["principal"].c.execution_id == claim["execution_id"])).mappings().one()
    operation = create(world.conn, "outbox_operation", actor_principal_id=principal["id"], project_id=world.project["id"],
        kind="forge_change", status=delivery_status, schema_version=1, idempotency_key="waiting-change", payload={})
    assert world.service.progress_stalled(world.conn, assignment["id"], 2)
    heartbeat = world.scheduler.heartbeat(world.conn, world.host_actor, claim["execution_id"], claim["epoch"])
    assert heartbeat["yield"] and not heartbeat["continue"]
    settled = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", claim["execution_id"]), "yielded")
    assert settled["status"] == "pending" and settled["checkpoint_requested_at"]
    assert settled["recovery_attempts"] == 0
    assert len(world.ledger(assignment["id"])) == 1
    assert world.claim() is None
    change(world.conn, "outbox_operation", operation["id"], status=terminal)
    assert not world.service.progress_stalled(world.conn, assignment["id"], 2)
    assert world.claim()["assignment_id"] == str(assignment["id"])


def test_settled_delivery_does_not_disable_future_no_progress_guard(world):
    run = world.run(retry_policy={"max_no_progress_requests": 2})
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    principal = world.conn.execute(select(tables["principal"]).where(
        tables["principal"].c.execution_id == claim["execution_id"])).mappings().one()
    now = world.conn.execute(select(func.now())).scalar_one()
    create(world.conn, "outbox_operation", actor_principal_id=principal["id"], project_id=world.project["id"],
        kind="forge_change", status="failed", schema_version=1, idempotency_key="failed-change", payload={},
        updated_at=now-timedelta(minutes=5))
    for number in (1, 2):
        create(world.conn, "provider_request", provider_thread_id=claim["provider_thread_record_id"],
            execution_id=claim["execution_id"], number=number, reason="continuation", status="completed",
            created_at=now-timedelta(minutes=3-number))
    assert world.service.progress_stalled(world.conn, assignment["id"], 2)
    settled = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", claim["execution_id"]), "yielded")
    assert settled["status"] == "failed"
    assert "reconsideration" in settled["status_note"]


def test_checkpoint_does_not_hide_actionable_failed_delivery(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    create(world.conn, "provider_request", provider_thread_id=claim["provider_thread_record_id"],
        execution_id=claim["execution_id"], number=1, reason="assignment", status="completed")
    principal = world.conn.execute(select(tables["principal"]).where(
        tables["principal"].c.execution_id == claim["execution_id"])).mappings().one()
    create(world.conn, "outbox_operation", actor_principal_id=principal["id"], project_id=world.project["id"],
        kind="forge_create", status="failed", schema_version=1, idempotency_key="actionable-failure", payload={})
    world.command("checkpoint_assignment", get(world.conn, "assignment", assignment["id"]), note="Wait for delivery")
    heartbeat = world.scheduler.heartbeat(world.conn, world.host_actor, claim["execution_id"], claim["epoch"])
    assert heartbeat["continue"] and not heartbeat["yield"]


@pytest.mark.parametrize("wake", ["delivery", "control"])
def test_delivery_checkpoint_wakes_on_partial_result_or_new_control_notice(world, wake):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    principal = world.conn.execute(select(tables["principal"]).where(
        tables["principal"].c.execution_id == claim["execution_id"])).mappings().one()
    operations = [create(world.conn, "outbox_operation", actor_principal_id=principal["id"],
        project_id=world.project["id"], kind="forge_create", schema_version=1,
        idempotency_key=f"waiting-{number}", payload={}) for number in range(2)]
    settled = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", claim["execution_id"]), "succeeded")
    assert world.claim() is None
    after_checkpoint = settled["checkpoint_requested_at"] + timedelta(seconds=1)
    if wake == "delivery":
        world.conn.execute(update(tables["outbox_operation"]).where(
            tables["outbox_operation"].c.id == operations[0]["id"]).values(status="failed", updated_at=after_checkpoint))
    else:
        from archon_horizon.pipeline.notifications import OperatorNotice, operator_notice
        notice = operator_notice(world.conn, world.actor, assignment["id"],
            OperatorNotice(message="Continue the independent API repair while delivery is investigated"))
        world.conn.execute(update(tables["notification"]).where(tables["notification"].c.id == notice["id"]).values(
            created_at=after_checkpoint))
    assert world.claim()["assignment_id"] == str(assignment["id"])


def test_elapsed_recovery_retry_is_admitted_before_ordinary_queue_work(world):
    run = world.run()
    world.disable_automations(run)
    ordinary = world.assignment(run)
    recovery = world.assignment(run)
    now = world.conn.execute(select(func.now())).scalar_one()
    # Put the later queue item into the recoverable state normally produced by
    # Scheduler.finish, then make its backoff elapsed.  It must not wait behind
    # ordinary work merely because its queue rank is newer.
    change(world.conn, "assignment", recovery["id"], retry_at=now - timedelta(seconds=1),
           status_note="Recoverable failure; preserved context will resume")
    claim = world.claim()
    assert claim["assignment_id"] == str(recovery["id"])
    assert get(world.conn, "assignment", ordinary["id"])["status"] == "pending"


def test_terminal_context_goal_update_is_superseded_during_admission(world):
    run = world.run()
    world.disable_automations(run)
    target = world.assignment(run)
    claim = world.claim()
    execution = get(world.conn, "execution", claim["execution_id"])
    world.scheduler.finish(world.conn, world.host_actor, execution, "cancelled")
    thread_id = claim["provider_thread_record_id"]
    principal = world.conn.execute(select(tables["principal"]).where(
        tables["principal"].c.execution_id == claim["execution_id"])).mappings().one()
    delivery = create(world.conn, "outbox_operation", project_id=world.project["id"],
        actor_principal_id=principal["id"], kind="goal_update", schema_version=1,
        idempotency_key="terminal-goal-update", payload={"provider_thread_id": str(thread_id)})
    successor = world.assignment(run)
    assert world.claim()["assignment_id"] == str(successor["id"])
    settled = get(world.conn, "outbox_operation", delivery["id"])
    assert settled["status"] == "cancelled"
    assert settled["failure"]["code"] == "target_terminal"


def test_whole_mission_single_successor_does_not_count_as_completion(world):
    run = world.run()
    world.disable_automations(run)
    parent = world.assignment(run)
    child = world.assignment(run, parent_id=parent["id"])
    root = world.ledger(parent["id"])[0]
    world.service.resolve_obligation(world.conn, world.actor, root["id"], models.ObligationResolve(
        expected_revision=root["revision"], status="handled", resolution={"kind":"scheduled",
            "assignment_id":child["id"], "note":"Pass the same mission to a new session"}))
    assert any("handed unchanged" in finding for finding in world.service.completion_findings(world.conn, parent["id"]))
    # A single child with a narrower objective remains a legitimate delegation.
    narrower = world.service.mission(world.conn, world.actor, models.MissionCreate(project_id=world.project["id"],
        parent_id=world.mission["id"], expected_parent_revision=get(world.conn, "mission", world.mission["id"])["revision"],
        acceptance_criteria=["Establish only the local estimate"], delegation_note="The remaining parent work stays with its owner",
        title="Supporting estimate", objective="Establish only the local estimate"))
    # Mission ownership is immutable: replace the test child with a genuinely scoped assignment.
    scoped = world.service.assignment(world.conn, world.actor, models.AssignmentCreate(
        run_id=run["id"], mission_id=narrower["id"], parent_id=parent["id"]))
    change(world.conn, "obligation", root["id"], resolution={"kind":"scheduled", "assignment_id":str(scoped["id"]), "note":"Narrow subproblem"})
    assert not world.service.completion_findings(world.conn, parent["id"])


def test_multiple_independent_delegations_are_allowed(world):
    run = world.run()
    world.disable_automations(run)
    parent = world.assignment(run)
    children = [world.assignment(run, parent_id=parent["id"], instructions=scope) for scope in ("Check statements", "Check proofs")]
    root = world.ledger(parent["id"])[0]
    world.service.resolve_obligation(world.conn, world.actor, root["id"], models.ObligationResolve(
        expected_revision=root["revision"], status="handled", resolution={"kind":"delegated",
            "assignment_ids":[child["id"] for child in children], "note":"Independent review scopes"}))
    assert not world.service.completion_findings(world.conn, parent["id"])


def test_operator_resume_reuses_settled_context(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    root = world.ledger(assignment["id"])[0]
    change(world.conn, "obligation", root["id"], status="done", resolution={"kind":"completed", "note":"Done", "evidence":[]})
    ended = world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", claim["execution_id"]), "succeeded")
    world.command("resume_assignment", ended, note="Integration work remains in this context")
    world.command("reopen_obligation", get(world.conn, "obligation", root["id"]), note="Continue integration")
    resumed = world.claim()
    assert resumed["provider_thread_record_id"] == claim["provider_thread_record_id"]


def checkpoint_for_later(world, assignment, claim, host_actor=None):
    change(world.conn, "provider_thread", claim["provider_thread_record_id"],
           provider_thread_id=f"retained-context-{assignment['id']}", status="available")
    world.command("checkpoint_assignment", get(world.conn, "assignment", assignment["id"]),
                  not_before=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), note="Await external work")
    return world.scheduler.finish(world.conn, host_actor or world.host_actor,
        get(world.conn, "execution", claim["execution_id"]), "yielded")


def test_new_assignment_prefers_workspace_without_retained_session(world):
    run = world.run()
    world.disable_automations(run)
    owner = world.assignment(run)
    first = world.claim()
    checkpoint_for_later(world, owner, first)
    newcomer = world.assignment(run)
    second = world.claim()
    assert second["assignment_id"] == str(newcomer["id"])
    assert second["workspace_id"] != first["workspace_id"]
    assert str(world.scheduler.available_workspace(world.conn, owner, world.host["id"])["id"]) == first["workspace_id"]
    # Reusing a stopped context's workspace is allowed when there is no
    # alternative; active executions must still never share their directory.
    for workspace in world.conn.execute(select(tables["workspace"])).mappings():
        if str(workspace["id"]) not in (first["workspace_id"], second["workspace_id"]):
            change(world.conn, "workspace", workspace["id"], status="unavailable")
    third = world.assignment(run)
    assert str(world.scheduler.available_workspace(world.conn, third, world.host["id"])["id"]) == first["workspace_id"]
    assert world.claim()["workspace_id"] == first["workspace_id"]
    assert world.scheduler.available_workspace(world.conn, owner, world.host["id"]) is None


def test_checkpointed_parents_do_not_exhaust_workspace_inventory_for_children(world):
    run = world.run()
    world.disable_automations(run)
    retained_workspaces = set()
    for _ in range(3):
        parent = world.assignment(run)
        claim = world.claim()
        retained_workspaces.add(claim["workspace_id"])
        checkpoint_for_later(world, parent, claim)
    assert len(retained_workspaces) == 3
    child = world.assignment(run, parent_id=parent["id"], instructions="Resolve the independent prerequisite")
    assert world.scheduler.admission_blocker(world.conn, child) is None
    claim = world.claim()
    assert claim["assignment_id"] == str(child["id"])
    assert claim["workspace_id"] in retained_workspaces


def test_retained_workspace_blocker_names_occupying_session_and_host(world, monkeypatch):
    run = world.run()
    world.disable_automations(run)
    owner = world.assignment(run)
    first = world.claim()
    checkpoint_for_later(world, owner, first)
    newcomer = world.assignment(run)
    # Represent an older installation where two retained sessions shared a worktree.
    with monkeypatch.context() as patch:
        patch.setattr(world.scheduler, "available_workspace", lambda *_: get(world.conn, "workspace", UUID(first["workspace_id"])))
        world.claim()
    blocker = world.scheduler.admission_blocker(world.conn, owner)
    assert f"R{run['number']}/A{newcomer['number']} is using this workspace" in blocker
    assert world.host["slug"] in blocker
    assert "free slots on other hosts cannot resume this context" in blocker


def test_retained_host_slot_blocker_is_distinct_from_workspace_contention(world):
    run = world.run()
    world.disable_automations(run)
    owner = world.assignment(run)
    first = world.claim()
    checkpoint_for_later(world, owner, first)
    world.assignment(run)
    second = world.claim()
    assert second["workspace_id"] != first["workspace_id"]
    hh = tables["host_harness"]
    world.conn.execute(update(hh).where(hh.c.host_id == world.host["id"]).values(execution_slots=1))
    blocker = world.scheduler.admission_blocker(world.conn, owner)
    assert f"{world.host['slug']}: all 1 compatible execution slots are occupied" == blocker


def other_run_host(world, run):
    host = create(world.conn, "host", slug=world.host["slug"] + "-other", display_name="Other",
        workspace_root=world.host["workspace_root"] + "-other", scratch_root=world.host["scratch_root"] + "-other",
        sandbox=world.host["sandbox"], heartbeat_at=func.now())
    principal = create(world.conn, "principal", kind="host", display_name="Other daemon", host_id=host["id"])
    actor = Actor(principal["id"], "host", {"host_id": str(host["id"])}, "host_key")
    config = dict(world.conn.execute(select(tables["host_harness"]).where(
        tables["host_harness"].c.host_id == world.host["id"])).mappings().one())
    world.conn.execute(insert(tables["host_harness"]).values(**{**config, "host_id": host["id"], "execution_slots": 1}))
    world.conn.execute(insert(tables["run_host"]).values(run_id=run["id"], host_id=host["id"]))
    create(world.conn, "workspace", project_id=world.project["id"], host_id=host["id"],
        repository_id=world.workspace_repo["id"], path=host["workspace_root"] + "/repo", branch_name="other",
        base_commit_oid="a" * 40, status="ready")
    return host, actor


def single_retained_local_workspace(world):
    run = world.run()
    world.disable_automations(run)
    parent = world.assignment(run)
    first = world.claim()
    checkpoint_for_later(world, parent, first)
    w = tables["workspace"]
    world.conn.execute(update(w).where(w.c.host_id == world.host["id"], w.c.id != UUID(first["workspace_id"]))
        .values(status="unavailable"))
    hh = tables["host_harness"]
    world.conn.execute(update(hh).where(hh.c.host_id == world.host["id"]).values(execution_slots=1))
    return run, parent, first


def test_new_assignment_prefers_free_unretained_workspace_on_another_host(world):
    run, parent, first = single_retained_local_workspace(world)
    other, actor = other_run_host(world, run)
    child = world.assignment(run, parent_id=parent["id"])
    assert world.claim() is None
    placed = world.scheduler.claim(world.conn, actor, other["id"], [world.harness["id"]])
    assert placed["assignment_id"] == str(child["id"])
    assert placed["workspace_id"] != first["workspace_id"]
    # The retained parent remains pinned to its original host and may resume
    # concurrently with the independently placed child.
    world.command("update_assignment", get(world.conn, "assignment", parent["id"]), not_before=None)
    resumed = world.claim()
    assert resumed["assignment_id"] == str(parent["id"])
    assert resumed["provider_thread_record_id"] == first["provider_thread_record_id"]


@pytest.mark.parametrize("alternative", ["full", "retained", "disabled"])
def test_cross_host_preference_falls_back_when_alternative_cannot_start_independently(world, alternative):
    run, parent, first = single_retained_local_workspace(world)
    other, actor = other_run_host(world, run)
    if alternative == "disabled":
        change(world.conn, "host", other["id"], mode="disabled")
    else:
        peer = world.assignment(run)
        claim = world.scheduler.claim(world.conn, actor, other["id"], [world.harness["id"]])
        if alternative == "retained":
            checkpoint_for_later(world, peer, claim, actor)
    child = world.assignment(run, parent_id=parent["id"])
    placed = world.claim()
    assert placed["assignment_id"] == str(child["id"])
    assert placed["workspace_id"] == first["workspace_id"]


def provisioned_child(world):
    run = world.run()
    world.disable_automations(run)
    change(world.conn, "host", world.host["id"], health={"status": "ready", "capabilities": {"workspace_preparation": 1}})
    parents = []
    for _ in range(3):
        parent = world.assignment(run)
        claim = world.claim()
        checkpoint_for_later(world, parent, claim)
        parents.append(claim)
    child = world.assignment(run, parent_id=parent["id"])
    return child, parents


def test_capable_worker_allocates_isolated_workspace_without_mutating_admission_queries(world):
    child, parents = provisioned_child(world)
    before = world.conn.execute(select(func.count()).select_from(tables["workspace"])).scalar_one()
    candidate = world.scheduler.available_workspace(world.conn, child, world.host["id"])
    assert candidate["status"] == "preparing"
    assert world.scheduler.admission_blocker(world.conn, child) is None
    assert world.conn.execute(select(func.count()).select_from(tables["workspace"])).scalar_one() == before
    claim = world.claim()
    assert claim["workspace_id"] == str(candidate["id"])
    assert claim["workspace_id"] not in {parent["workspace_id"] for parent in parents}
    assert claim["workspace_path"] == world.host["workspace_root"] + "/assignments/" + str(child["id"])
    preparation = claim["workspace_preparation"]
    source = get(world.conn, "workspace", preparation["source_workspace_id"])
    assert preparation == {"schema_version": 1, "source_workspace_id": str(source["id"]), "source_path": source["path"],
                           "commit_oid": source["base_commit_oid"], "branch_name": "horizon/assignments/" + str(child["id"])}
    assert get(world.conn, "workspace", claim["workspace_id"])["status"] == "preparing"


def test_workspace_preparation_retry_keeps_context_directory_and_pinned_commit(world):
    child, _ = provisioned_child(world)
    first = world.claim()
    world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", first["execution_id"]), "failed",
        {"kind": "transport", "code": "connection_error", "message": "Preparation interrupted"})
    change(world.conn, "assignment", child["id"], retry_at=None)
    resumed = world.claim()
    assert resumed["workspace_id"] == first["workspace_id"]
    assert resumed["provider_thread_record_id"] == first["provider_thread_record_id"]
    assert resumed["workspace_preparation"] == first["workspace_preparation"]


def preparation_receipt(world, claim, **changes):
    preparation = claim["workspace_preparation"]
    operation = WorkerOperation(operation_id=uuid4(), execution_id=UUID(claim["execution_id"]), epoch=claim["epoch"],
        kind="workspace_prepared", payload={"workspace_id": claim["workspace_id"],
            "head_commit_oid": preparation["commit_oid"], "branch_name": preparation["branch_name"], **changes},
        occurred_at=datetime.now(timezone.utc).timestamp())
    return worker_event(world.conn, world.host_actor, operation, world.service, world.scheduler)


def test_workspace_preparation_requires_exact_execution_directory_commit_and_branch(world):
    _, parents = provisioned_child(world)
    claim = world.claim()
    for changes, code in (({"workspace_id": parents[0]["workspace_id"]}, "scope_mismatch"),
                          ({"head_commit_oid": "b" * 40}, "workspace_snapshot_mismatch"),
                          ({"branch_name": "main"}, "workspace_snapshot_mismatch")):
        with pytest.raises(DomainError) as error:
            preparation_receipt(world, claim, **changes)
        assert error.value.code == code
        assert get(world.conn, "workspace", claim["workspace_id"])["status"] == "preparing"
    first = preparation_receipt(world, claim)
    assert first["status"] == "ready"
    revision = get(world.conn, "workspace", claim["workspace_id"])["revision"]
    assert preparation_receipt(world, claim) == first
    assert get(world.conn, "workspace", claim["workspace_id"])["revision"] == revision
    change(world.conn, "execution", claim["execution_id"], status="lost")
    with pytest.raises(DomainError) as error:
        preparation_receipt(world, claim)
    assert error.value.code == "stale_epoch"


def test_completed_context_cannot_resume_into_another_unfinished_contexts_workspace(world):
    run = world.run()
    world.disable_automations(run)
    original = world.assignment(run)
    first = world.claim()
    root = world.ledger(original["id"])[0]
    change(world.conn, "obligation", root["id"], status="done",
           resolution={"kind": "completed", "note": "Done", "evidence": []})
    settled = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", first["execution_id"]), "succeeded")
    # Settled workspaces remain reusable instead of accumulating a full checkout
    # for every historical assignment, but an explicit resume must respect reuse.
    for workspace in world.conn.execute(select(tables["workspace"])).mappings():
        if str(workspace["id"]) != first["workspace_id"]:
            change(world.conn, "workspace", workspace["id"], status="unavailable")
    replacement = world.assignment(run)
    second = world.claim()
    assert second["workspace_id"] == first["workspace_id"]
    checkpoint_for_later(world, replacement, second)
    with pytest.raises(DomainError) as error:
        world.command("resume_assignment", settled, note="Additional work remains")
    assert error.value.code == "workspace_reassigned"
    assert get(world.conn, "assignment", original["id"])["status"] == "completed"


def test_provisioning_hosts_do_not_defer_to_each_other_when_both_need_new_workspaces(world):
    run, parent, first = single_retained_local_workspace(world)
    other, actor = other_run_host(world, run)
    peer = world.assignment(run)
    second = world.scheduler.claim(world.conn, actor, other["id"], [world.harness["id"]])
    checkpoint_for_later(world, peer, second, actor)
    for host in (world.host, other):
        change(world.conn, "host", host["id"], health={"status": "ready", "capabilities": {"workspace_preparation": 1}})
    child = world.assignment(run, parent_id=parent["id"])
    claim = world.claim()
    assert claim["assignment_id"] == str(child["id"])
    assert claim["workspace_preparation"]
    assert claim["workspace_id"] not in (first["workspace_id"], second["workspace_id"])


def test_new_workspace_uses_latest_verified_source_head_and_pins_it_for_retry(world):
    child, _ = provisioned_child(world)
    source = world.conn.execute(select(tables["workspace"]).where(tables["workspace"].c.host_id == world.host["id"])
        .order_by(tables["workspace"].c.created_at, tables["workspace"].c.id).limit(1)).mappings().one()
    change(world.conn, "workspace", source["id"], head_commit_oid="b" * 40)
    claim = world.claim()
    assert claim["workspace_preparation"]["commit_oid"] == "b" * 40
    change(world.conn, "workspace", source["id"], head_commit_oid="c" * 40)
    world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", claim["execution_id"]), "failed",
        {"kind": "transport", "code": "connection_error", "message": "Interrupted"})
    change(world.conn, "assignment", child["id"], retry_at=None)
    assert world.claim()["workspace_preparation"]["commit_oid"] == "b" * 40


def test_postprocessing_new_workspace_uses_run_source_pin_not_later_head(world):
    provisioned_child(world)
    library = create(world.conn, "repository", project_id=world.project["id"], slug="library",
        integration_id=world.workspace_repo["integration_id"], remote_id="library", default_branch="main", purpose="library")
    policy = create(world.conn, "review_policy", project_id=world.project["id"], slug="library",
        phases=["postprocessing"], instructions="Review statements")
    world.conn.execute(insert(tables["review_policy_repository"]).values(
        review_policy_id=policy["id"], repository_id=library["id"]))
    source = world.conn.execute(select(tables["workspace"]).where(tables["workspace"].c.host_id == world.host["id"])
        .order_by(tables["workspace"].c.created_at, tables["workspace"].c.id).limit(1)).mappings().one()
    change(world.conn, "workspace", source["id"], head_commit_oid="b" * 40)
    run = world.scheduler.run(world.conn, world.actor, models.RunCreate(mission_id=world.mission["id"],
        host_ids=[world.host["id"]], phase={"kind": "postprocessing", "source_workspace_id": source["id"],
            "source_commit_oid": "b" * 40, "target_repository_id": library["id"]}))
    world.disable_automations(run)
    change(world.conn, "workspace", source["id"], head_commit_oid="c" * 40)
    child = world.assignment(run)
    proposal = world.scheduler.available_workspace(world.conn, child, world.host["id"])
    assert proposal["base_commit_oid"] == "b" * 40
    assert proposal["repository_id"] == source["repository_id"]
