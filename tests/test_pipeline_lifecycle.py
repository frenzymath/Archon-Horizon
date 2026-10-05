from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from archon_horizon.pipeline.catalog import CatalogUpdate, create_catalog, update_catalog
from archon_horizon.pipeline.models import ObligationResolve
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.records import change, create, get
from archon_horizon.pipeline.reviews import queue_review
from archon_horizon.pipeline.schema import tables

from test_pipeline_service import service_database, world


def test_node_edits_reject_cycles_and_preserve_revision_conflicts(world):
    def node(title):
        return create_catalog(world.conn, world.actor, "node", {"project_id": world.project["id"],
            "title": title, "source_repository_id": world.workspace_repo["id"], "source_path": title + ".md",
            "source_commit_oid": "a" * 40})
    first, second = node("First"), node("Second")
    update_catalog(world.conn, world.actor, "node", first["id"], CatalogUpdate(expected_revision=1,
        changes={"parent_node_ids": [second["id"]]}))
    with pytest.raises(DomainError, match="[Cc]ycl"):
        update_catalog(world.conn, world.actor, "node", second["id"], CatalogUpdate(expected_revision=1,
            changes={"parent_node_ids": [first["id"]]}))
    with pytest.raises(DomainError) as error:
        update_catalog(world.conn, world.actor, "node", first["id"], CatalogUpdate(expected_revision=1,
            changes={"title": "Stale overwrite"}))
    assert error.value.code == "revision_conflict"
    assert get(world.conn, "node", first["id"])["title"] == "First"


def test_catalog_cannot_move_identity_or_overlap_review_policy(world):
    with pytest.raises(DomainError) as error:
        update_catalog(world.conn, world.actor, "repository", world.workspace_repo["id"],
            CatalogUpdate(expected_revision=1, changes={"project_id": world.project["id"]}))
    assert error.value.code == "immutable_fields"
    second = create_catalog(world.conn, world.actor, "review_policy", {"project_id": world.project["id"],
        "slug": "second", "repository_ids": [world.document["source_repository_id"]],
        "phases": ["postprocessing"], "instructions": "Review"})
    with pytest.raises(DomainError) as error:
        update_catalog(world.conn, world.actor, "review_policy", second["id"],
            CatalogUpdate(expected_revision=1, changes={"phases": ["preprocessing"]}))
    assert error.value.code == "ambiguous_review_policy"


def test_context_recovery_preserves_workspace_and_ledger_and_requires_stopped_execution(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run, role="maintainer")
    claim = world.claim()
    thread = get(world.conn, "provider_thread", claim["provider_thread_record_id"])
    with pytest.raises(DomainError) as error:
        world.command("recover_context", thread, note="Native state unavailable")
    assert error.value.code == "execution_active"
    world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", claim["execution_id"]),
                           "failed", {"kind": "provider", "code": "execution_failed", "message": "Lost native state"})
    old = change(world.conn, "provider_thread", thread["id"], status="unavailable")
    replacement = world.command("recover_context", old, note="Confirmed native state cannot be resumed")
    assert replacement["predecessor_id"] == old["id"]
    assert replacement["workspace_id"] == old["workspace_id"]
    assert get(world.conn, "provider_thread", old["id"])["status"] == "closed"
    assert world.ledger(assignment["id"])[0]["status"] == "open"
    assert world.scheduler.tick(world.conn)["retired_maintainers"] == 0
    assert world.claim()["provider_thread_record_id"] == str(replacement["id"])


def test_manual_maintainer_with_unavailable_context_waits_for_explicit_recovery(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run, role="maintainer")
    claim = world.claim()
    world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", claim["execution_id"]),
                           "failed", {"kind": "transport", "code": "connection_error", "message": "Connection interrupted"})
    thread = change(world.conn, "provider_thread", claim["provider_thread_record_id"], status="unavailable")
    assert world.scheduler.tick(world.conn)["retired_maintainers"] == 0
    pending = get(world.conn, "assignment", assignment["id"])
    assert pending["status"] == "pending"
    assert pending["automation_id"] is None
    assert world.claim() is None
    replacement = world.command("recover_context", thread, note="Native state cannot be resumed")
    assert world.claim()["provider_thread_record_id"] == str(replacement["id"])


def test_draining_finishes_existing_work_but_only_accepts_explicit_repairs(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    world.command("complete_mission", world.mission, note="Main result attained; finishing tracked cleanup")
    draining = world.command("drain_run", run)
    assert UUID(world.claim()["assignment_id"]) == assignment["id"]
    with pytest.raises(DomainError) as error:
        world.assignment(run)
    assert error.value.code == "run_not_active"
    repair = world.command("queue_repair", draining, note="Correct a discovered integration issue",
        assignment={"run_id": str(run["id"]), "mission_id": str(world.mission["id"]), "instructions": "Repair"})
    assert repair["run_id"] == run["id"]


def test_run_completion_cannot_hide_failed_unhandled_work(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    change(world.conn, "assignment", assignment["id"], status="failed")
    world.command("complete_mission", world.mission, note="Semantic result attained")
    draining = world.command("drain_run", run)
    with pytest.raises(DomainError) as error:
        world.command("complete_run", draining)
    assert error.value.code == "run_unsettled"


@pytest.mark.parametrize("blocker", ["physical_stop", "external_delivery"])
def test_run_completion_waits_for_physical_stop_and_external_receipts(world, blocker):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    grant = world.claim()
    execution_id = UUID(grant["execution_id"])
    obligation = world.ledger(assignment["id"])[0]
    world.service.resolve_obligation(world.conn, world.actor, obligation["id"], ObligationResolve(
        expected_revision=obligation["revision"], status="done",
        resolution={"kind": "completed", "note": "Semantic work finished", "evidence": []}))
    change(world.conn, "assignment", assignment["id"], status="completed")
    change(world.conn, "execution", execution_id, status="lost",
        stop_confirmed_at=None if blocker == "physical_stop" else func.now())
    delivery = None
    if blocker == "external_delivery":
        principal = tables["principal"]
        agent_id = world.conn.execute(select(principal.c.id).where(principal.c.execution_id == execution_id)).scalar_one()
        delivery = create(world.conn, "outbox_operation", project_id=world.project["id"],
            actor_principal_id=agent_id, kind="forge_comment", idempotency_key="final-comment",
            schema_version=1, payload={"body": "Completed result"}, status="uncertain")
    world.command("complete_mission", world.mission, note="Semantic result attained")
    draining = world.command("drain_run", run)
    with pytest.raises(DomainError) as error:
        world.command("complete_run", draining)
    assert error.value.code == "run_unsettled"
    change(world.conn, "execution", execution_id, stop_confirmed_at=func.now())
    if delivery:
        change(world.conn, "outbox_operation", delivery["id"], status="completed")
    assert world.command("complete_run", draining)["status"] == "completed"


def test_draining_run_automatically_completes_after_last_maintainer_stops(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run, role="maintainer")
    grant = world.claim()
    obligation = world.ledger(assignment["id"])[0]
    world.service.resolve_obligation(world.conn, world.actor, obligation["id"], ObligationResolve(
        expected_revision=obligation["revision"], status="done",
        resolution={"kind": "completed", "note": "All results reviewed and published", "evidence": []}))
    world.command("complete_mission", world.mission, note="The full mathematical objective is complete")
    world.command("drain_run", run)
    assert world.scheduler.tick(world.conn)["completed"] == 0
    world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", grant["execution_id"]), "succeeded")
    assert world.scheduler.tick(world.conn)["completed"] == 1
    completed = get(world.conn, "run", run["id"])
    assert completed["status"] == "completed" and completed["finished_at"] is not None
    assert world.scheduler.tick(world.conn)["completed"] == 0
    assert get(world.conn, "run", run["id"])["revision"] == completed["revision"]


@pytest.mark.parametrize("blocker", ["mission", "assignment", "obligation", "physical_stop", "publication", "delivery"])
def test_automatic_run_completion_keeps_every_settlement_guard(world, blocker):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    grant = world.claim()
    execution_id = UUID(grant["execution_id"])
    obligation = world.ledger(assignment["id"])[0]
    change(world.conn, "obligation", obligation["id"], status="done",
        resolution={"kind": "completed", "note": "Work finished"})
    change(world.conn, "assignment", assignment["id"], status="completed")
    change(world.conn, "execution", execution_id, status="succeeded", stop_confirmed_at=func.now())
    mission = world.command("complete_mission", world.mission, note="Objective fulfilled")
    world.command("drain_run", run)
    if blocker == "mission":
        blocked = world.command("reopen_mission", mission, note="A mathematical gap was discovered")
    elif blocker == "assignment":
        blocked = change(world.conn, "assignment", assignment["id"], status="pending")
    elif blocker == "obligation":
        blocked = change(world.conn, "obligation", obligation["id"], status="open", resolution=None)
    elif blocker == "physical_stop":
        blocked = change(world.conn, "execution", execution_id, status="lost", stop_confirmed_at=None)
    elif blocker == "publication":
        artifact = create(world.conn, "artifact", project_id=world.project["id"], kind="commit",
            content={"repository_id": str(world.workspace_repo["id"]), "commit_oid": "a" * 40})
        blocked = create(world.conn, "publication", artifact_id=artifact["id"],
            requested_by_assignment_id=assignment["id"], status="failed",
            target={"kind": "git", "repository_id": str(world.workspace_repo["id"]),
                    "ref_name": "refs/heads/horizon/recovery/test"})
    else:
        principal = tables["principal"]
        agent_id = world.conn.execute(select(principal.c.id).where(principal.c.execution_id == execution_id)).scalar_one()
        blocked = create(world.conn, "outbox_operation", project_id=world.project["id"],
            actor_principal_id=agent_id, kind="forge_merge", idempotency_key="last-merge",
            schema_version=1, payload={}, status="uncertain")
    assert world.scheduler.tick(world.conn)["completed"] == 0
    assert get(world.conn, "run", run["id"])["status"] == "draining"
    if blocker == "mission":
        world.command("complete_mission", blocked, note="Gap resolved")
    elif blocker == "assignment":
        change(world.conn, "assignment", blocked["id"], status="completed")
    elif blocker == "obligation":
        change(world.conn, "obligation", blocked["id"], status="done",
            resolution={"kind": "completed", "note": "Missing work completed"})
    elif blocker == "physical_stop":
        change(world.conn, "execution", blocked["id"], stop_confirmed_at=func.now())
    elif blocker == "publication":
        change(world.conn, "publication", blocked["id"], status="verified", verified_at=func.now())
    else:
        change(world.conn, "outbox_operation", blocked["id"], status="completed")
    assert world.scheduler.tick(world.conn)["completed"] == 1


def test_scheduler_does_not_infer_run_completion_from_empty_queue(world):
    run = world.run()
    world.disable_automations(run)
    world.command("complete_mission", world.mission, note="Objective fulfilled")
    assert world.scheduler.tick(world.conn)["completed"] == 0
    assert get(world.conn, "run", run["id"])["status"] == "active"


def test_policy_selects_separate_maintainer_identity_for_approval(world):
    identity = create(world.conn, "integration_identity", integration_id=world.workspace_repo["integration_id"],
        principal_id=world.actor.id, remote_user_id="maintainer", credential_ref="secret:maintainer")
    change(world.conn, "review_policy", world.policy["id"], maintainer_identity_id=identity["id"])
    item = create(world.conn, "forge_item", repository_id=world.document["source_repository_id"], remote_number=42,
        kind="pull_request", review_phase="preprocessing", title="Milestones", status="open", head_commit_oid="a" * 40,
        observed_at=world.conn.execute(select(tables["project"].c.created_at).where(
            tables["project"].c.id == world.project["id"])).scalar_one())
    delivery = queue_review(world.conn, world.actor, world.service, world.scheduler,
        {"forge_item_id": str(item["id"]), "commit_oid": "a" * 40, "verdict": "approved", "summary": "Reviewed"}, "approval")
    assert delivery["payload"]["integration_identity_id"] == str(identity["id"])


def test_delivery_recovery_requires_settled_outcome_and_retains_same_remote_marker(world):
    intent = create(world.conn, "outbox_operation", project_id=world.project["id"], actor_principal_id=world.actor.id,
        kind="forge_comment", idempotency_key="comment", schema_version=1, payload={"body": "Result"}, status="uncertain")
    with pytest.raises(DomainError) as error:
        world.command("retry_delivery", intent, note="Network recovered")
    assert error.value.code == "delivery_unsettled"
    intent = change(world.conn, "outbox_operation", intent["id"], status="failed")
    retried = world.command("retry_delivery", intent, note="Confirmed no remote effect; repaired credentials")
    assert retried["id"] == intent["id"]
    assert retried["payload"] == intent["payload"]
    cancelled = world.command("cancel_delivery", retried, note="Superseded by a corrected report")
    assert cancelled["status"] == "cancelled"
