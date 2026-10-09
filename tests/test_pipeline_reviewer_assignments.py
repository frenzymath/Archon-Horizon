from datetime import datetime, timezone
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.persistence.records import change, create, get
from archon_horizon.pipeline.review.invocations import assignment_manifest, assignment_request_fields, prepare_assignment
from archon_horizon.pipeline.review.decisions import queue_review
from archon_horizon.pipeline.persistence.schema import tables
from archon_horizon.pipeline.execution.worker_events import WorkerOperation, handle
from test_pipeline_reviewer_invocations import review, used  # noqa: F401
from test_pipeline_service import service_database, world  # noqa: F401


def test_queued_review_pins_rubric_without_native_capacity(world, review):
    world.conn.execute(update(tables["host_harness"]).values(max_parallel_subagents=0))
    change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], invocation="assignment")
    result = prepare_assignment(world.conn, review["actor"], review["data"], world.service)
    assignment = result["assignment"]
    assert assignment["status"] == "pending" and assignment["role"] == "worker"
    assert assignment["parent_id"] == review["assignment"]["id"]
    assert assignment["harness_id"] == world.harness["id"]
    assert assignment["mission_id"] != review["assignment"]["mission_id"]
    assert result["completion_condition"] == {"version": 1, "expression": {"op": "status_in",
        "target": {"kind": "assignment", "id": str(assignment["id"])},
        "values": ["completed", "failed", "cancelled"]}}
    assert "complete_mission" in result["prompt"]
    assert str(assignment["mission_id"]) in result["prompt"]
    assert "Do not wait for the PR to merge" in result["prompt"]
    assert "YOUR_RESULT_AND_VERIFIED_RECEIPT_URL" in result["prompt"]
    assert "evidence:[{kind:provider_request,id:YOUR_CURRENT_HORIZON_PROVIDER_REQUEST_ID}]" in result["prompt"]
    assert "Then finish your native review turn" not in result["prompt"]
    assert used(world, review) == 1
    artifact, manifest = assignment_manifest(world.conn, world.service, assignment)
    assert artifact["id"] == result["manifest_artifact_id"]
    assert manifest["head_commit_oid"] == "a" * 40
    assert manifest["guidance"][0]["source_commit_oid"] == "a" * 40
    change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], instructions="Changed later", model_options={"model": "changed"})
    change(world.conn, "document", world.document["id"], source_commit_oid="b" * 40)
    assert assignment_manifest(world.conn, world.service, assignment)[1] == manifest
    fields = assignment_request_fields(world.conn, world.service, assignment)
    assert fields["reason"] == "review"
    assert fields["reviewer_descriptor_revision_id"] == UUID(manifest["reviewer_descriptor_revision_id"])
    obligation = get(world.conn, "obligation", result["obligation_id"])
    assert obligation["status"] == "open"
    assert str(assignment["id"]) in obligation["description"]
    with pytest.raises(IntegrityError), world.conn.begin_nested():
        world.conn.execute(update(tables["assignment_artifact"]).where(
            tables["assignment_artifact"].c.assignment_id == assignment["id"]).values(purpose="evidence"))


def test_queued_review_reuses_mission_assignment_and_obligation(world, review):
    change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], invocation="assignment")
    first = prepare_assignment(world.conn, review["actor"], review["data"], world.service)
    mission_count = world.conn.execute(select(func.count()).select_from(tables["mission"])).scalar_one()
    second = prepare_assignment(world.conn, review["actor"], review["data"], world.service)
    assert second["assignment"]["id"] == first["assignment"]["id"]
    assert second["obligation_id"] == first["obligation_id"]
    assert second["completion_condition"] == first["completion_condition"]
    assert world.conn.execute(select(func.count()).select_from(tables["mission"])).scalar_one() == mission_count


def test_unprepared_or_forged_reviewer_assignment_is_not_dispatched_as_review(world, review):
    with pytest.raises(DomainError) as error:
        world.assignment(review["run"], reviewer_descriptor_id=review["descriptor"]["id"])
    assert error.value.code == "review_manifest_required"
    assignment = create(world.conn, "assignment", run_id=review["run"]["id"], mission_id=world.mission["id"],
        reviewer_descriptor_id=review["descriptor"]["id"], number=99, queue_rank=99000)
    with pytest.raises(DomainError) as error:
        assignment_manifest(world.conn, world.service, assignment)
    assert error.value.code == "review_manifest_missing"
    change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], invocation="assignment")
    prepared = prepare_assignment(world.conn, review["actor"], review["data"], world.service)
    changed = dict(prepared["assignment"], reviewer_descriptor_id=assignment["id"])
    with pytest.raises(DomainError) as error:
        assignment_manifest(world.conn, world.service, changed)
    assert error.value.code == "review_manifest_mismatch"


def test_queued_review_reuses_native_scope_and_exact_head_validation(world, review):
    change(world.conn, "forge_item", review["item"]["id"], head_commit_oid="c" * 40)
    with pytest.raises(DomainError) as error:
        prepare_assignment(world.conn, review["actor"], review["data"], world.service)
    assert error.value.code == "review_head_changed"
    assert used(world, review) == 1


def test_native_descriptor_cannot_silently_become_a_queued_reviewer(world, review):
    world.conn.execute(update(tables["host_harness"]).values(max_parallel_subagents=0))
    with pytest.raises(DomainError) as error:
        prepare_assignment(world.conn, review["actor"], review["data"], world.service)
    assert error.value.code == "reviewer_requires_native"
    assert used(world, review) == 1


def test_scheduled_reviewer_keeps_pins_through_dispatch_and_review_publication(world, review):
    world.conn.execute(update(tables["host_harness"]).values(max_parallel_subagents=0))
    change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], invocation="assignment")
    prepared = prepare_assignment(world.conn, review["actor"], review["data"], world.service)
    assignment = prepared["assignment"]
    _, manifest = assignment_manifest(world.conn, world.service, assignment)
    change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], instructions="UNPINNED RUBRIC", model_options={"model": "unselected"})
    change(world.conn, "harness", world.harness["id"], provider_version="newer-unpinned", model_options={"model": "new-default"})
    change(world.conn, "document", world.document["id"], source_commit_oid="b" * 40)
    grant = world.claim()
    assert grant["assignment_id"] == str(assignment["id"])
    assert grant["max_parallel_subagents"] == 0
    assert grant["harness_configuration"]["model_options"]["model"] == "review-model"
    assert grant["harness_configuration"]["provider_version"] == "1"
    execution = get(world.conn, "execution", grant["execution_id"])
    assert str(execution["skill_bundle_artifact_id"]) == manifest["skill_bundle_artifact_id"]
    assert str(execution["harness_revision_id"]) == manifest["harness_revision_id"]
    prompt = grant["goal"]
    if grant["goal_artifact_id"]:
        blob = get(world.conn, "artifact", grant["goal_artifact_id"])
        prompt = json.loads(world.service.store.read(blob["content"]["sha256"]))["goal"]
    assert "Inspect mathematical source alignment" in prompt and "UNPINNED RUBRIC" not in prompt
    assert "a" * 40 in prompt
    request_id = uuid4()

    def observe(event, **payload):
        return handle(world.conn, world.host_actor, WorkerOperation(operation_id=uuid4(),
            execution_id=execution["id"], epoch=execution["number"], kind="provider_observed",
            payload={"event": event, "request_id": str(request_id),
                     "provider_thread_record_id": grant["provider_thread_record_id"], **payload},
            occurred_at=datetime.now(timezone.utc).timestamp()), world.service, world.scheduler)

    observe("request_started", goal=prompt, mission_revision_id=grant["mission_revision_id"],
            mission_revision_number=grant["mission_revision_number"], run_revision=grant["run_revision"])
    request = get(world.conn, "provider_request", request_id)
    assert request["reason"] == "review"
    assert str(request["reviewer_descriptor_revision_id"]) == manifest["reviewer_descriptor_revision_id"]
    assert request["guidance_manifest_artifact_id"] == prepared["manifest_artifact_id"]
    observe("request_completed", status="completed", provider_thread_id="queued-native-review")
    raw = {"forge_item_id": str(review["item"]["id"]), "commit_oid": "a" * 40, "verdict": "commented",
           "summary": "Statement review found no blockers", "reviewer_descriptor_id": str(review["descriptor"]["id"]),
           "provider_request_id": str(request_id)}
    outbox = queue_review(world.conn, review["actor"], world.service, world.scheduler, raw, "queued-review")
    assert outbox["payload"]["reviewer_descriptor_revision_id"] == manifest["reviewer_descriptor_revision_id"]
    assert outbox["payload"]["policy_revision_id"] == manifest["policy_revision_id"]
    assert outbox["payload"]["commit_oid"] == "a" * 40
    change(world.conn, "forge_item", review["item"]["id"], head_commit_oid="c" * 40)
    with pytest.raises(DomainError) as error:
        queue_review(world.conn, review["actor"], world.service, world.scheduler, {**raw, "commit_oid": "c" * 40}, "wrong-head")
    assert error.value.code == "review_head_changed"
    with pytest.raises(DomainError):
        world.command("update_assignment", get(world.conn, "assignment", assignment["id"]), harness_id=str(uuid4()))
