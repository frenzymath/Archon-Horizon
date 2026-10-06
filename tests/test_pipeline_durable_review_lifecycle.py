"""A strict contract panel can finish after its dispatching maintainer exits."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import insert, select, update

from archon_horizon.pipeline import models
from archon_horizon.pipeline.auth import authenticate
from archon_horizon.pipeline.commands import Command, execute
from archon_horizon.pipeline.records import change, create, get, object_ref
from archon_horizon.pipeline.reviewer_invocations import ReviewerReport, prepare_assignment, report
from archon_horizon.pipeline.reviews import queue_review, review_readiness
from archon_horizon.pipeline.schema import tables
from archon_horizon.pipeline.worker_events import WorkerOperation, handle
from test_pipeline_milestones import reviewed_gate
from test_pipeline_review_assessments import assessment, deliver
from test_pipeline_reviewer_invocations import review, used  # noqa: F401
from test_pipeline_reviewer_reports import reviewer_account
from test_pipeline_service import service_database, world  # noqa: F401


def observe(world, grant, request_id, event, **payload):
    return handle(world.conn, world.host_actor, WorkerOperation(operation_id=uuid4(),
        execution_id=grant["execution_id"], epoch=grant["epoch"], kind="provider_observed",
        payload={"event": event, "request_id": str(request_id),
                 "provider_thread_record_id": grant["provider_thread_record_id"], **payload},
        occurred_at=datetime.now(timezone.utc).timestamp()), world.service, world.scheduler)


def settle(world, actor, obligation, *, owners=None, evidence=None):
    resolution = ({"kind": "delegated", "assignment_ids": owners,
                   "note": "Exact-head specialists own their assessments; integration has a terminal-condition owner."}
                  if owners else {"kind": "completed", "note": "Delivered the bounded review with its verified receipt.",
                                  "evidence": evidence or []})
    return world.service.resolve_obligation(world.conn, actor, obligation["id"], models.ObligationResolve(
        expected_revision=obligation["revision"], status="handled" if owners else "done", resolution=resolution))


def deliver_labels(world, item):
    operations = tables["outbox_operation"]
    for row in world.conn.execute(select(operations).where(operations.c.kind == "forge_label",
            operations.c.status == "pending")).mappings():
        change(world.conn, "outbox_operation", row["id"], status="completed",
               result_ref_id=object_ref(world.conn, "forge_item", item["id"]))


def test_four_contract_reviews_outlive_parent_and_wake_one_integration_owner(world, review):
    change(world.conn, "project", world.project["id"], workflow="milestones")
    world.conn.execute(update(tables["host_harness"]).values(execution_slots=3, max_parallel_subagents=0))
    identity = reviewer_account(world, review)
    item = review["item"]
    route, _ = reviewed_gate(world, review, "b" * 40, 2, ["decomposition"])
    for head, kind, ancestors in ((route["head_commit_oid"], "route", []),
                                   (item["head_commit_oid"], "contract", [route["head_commit_oid"]])):
        create(world.conn, "milestone_check", project_id=world.project["id"], repository_id=item["repository_id"],
            principal_id=world.actor.id, source_commit_oid=head, base_commit_oid="c" * 40, kind=kind,
            report={"ancestor_commits": ancestors, "milestone_keys": ["M01"], "compiled": True},
            report_sha256=head[:1] * 64)
    perspectives = ["statement-fidelity", "definitions", "decomposition", "library-api"]
    prepared = []
    for index, name in enumerate(perspectives):
        if index == 0:
            descriptor = change(world.conn, "reviewer_descriptor", review["descriptor"]["id"],
                slug=name, invocation="assignment")
        else:
            descriptor = create(world.conn, "reviewer_descriptor", project_id=world.project["id"], slug=name,
                invocation="assignment", functions=["reviewer"], instructions=f"Independently inspect {name}",
                integration_identity_id=identity["id"])
            world.conn.execute(insert(tables["review_policy_reviewer"]).values(
                review_policy_id=world.policy["id"], reviewer_descriptor_id=descriptor["id"]))
        prepared.append(prepare_assignment(world.conn, review["actor"], review["data"].model_copy(
            update={"reviewer_descriptor_id": descriptor["id"]}), world.service))
    assert used(world, review) == 1
    assert all(row["assignment"]["status"] == "pending" for row in prepared)
    assert not review_readiness(world.conn, item)["ready"]

    parent_mission = get(world.conn, "mission", review["assignment"]["mission_id"])
    integration_mission = world.service.mission(world.conn, review["actor"], models.MissionCreate(
        project_id=world.project["id"], parent_id=parent_mission["id"],
        expected_parent_revision=parent_mission["revision"], title="Integrate the reviewed contract",
        objective="Inspect the four delivered contract assessments and decide the pinned PR.",
        acceptance_criteria=["A current-head maintainer decision or a concrete repair owner"],
        delegation_note="Integration becomes runnable when every independent review attempt settles."))
    condition = {"op": "all", "args": [row["completion_condition"]["expression"] for row in prepared]}
    integration = world.service.assignment(world.conn, review["actor"], models.AssignmentCreate(
        run_id=review["run"]["id"], mission_id=integration_mission["id"],
        parent_id=review["assignment"]["id"], role="maintainer", start_condition={"expression": condition}))
    for obligation in world.ledger(review["assignment"]["id"]):
        if obligation["number"] == 1:
            owners = [row["assignment"]["id"] for row in prepared] + [integration["id"]]
        else:
            owners = [next(row["assignment"]["id"] for row in prepared
                           if row["obligation_id"] == obligation["id"])]
        settle(world, review["actor"], obligation, owners=owners)
    observe(world, review["grant"], review["request"]["id"], "request_completed", status="completed")
    deliver_labels(world, item)
    finished = world.scheduler.finish(world.conn, world.host_actor, review["execution"], "succeeded")
    assert finished["status"] == "completed", (finished["status_note"],
        world.service.completion_findings(world.conn, finished["id"]))
    assert used(world, review) == 0

    # Three physical primary slots admit three reviewers; the fourth waits
    # without tying up a maintainer or allocating a native child reservation.
    active = [world.claim() for _ in range(3)]
    assert all(grant and grant["role"] == "worker" for grant in active)
    assert world.claim() is None and used(world, review) == 3
    reviewed = []
    while active:
        grant = active.pop(0)
        actor = authenticate(world.conn, grant["execution_token"])
        assignment = get(world.conn, "assignment", grant["assignment_id"])
        request_id = uuid4()
        observe(world, grant, request_id, "request_started", goal="Assess the pinned contract",
            mission_revision_id=grant["mission_revision_id"], mission_revision_number=grant["mission_revision_number"],
            run_revision=grant["run_revision"])
        operation = report(world.conn, actor, request_id, ReviewerReport(verdict="approved", assessment=assessment()),
            world.service, "durable-" + str(request_id))
        receipt = deliver(world, {**review, "actor": actor}, identity, operation)
        deliver_labels(world, item)
        assert get(world.conn, "provider_request", request_id)["status"] == "submitted"
        assert not world.service.readiness(world.conn, get(world.conn, "assignment", integration["id"]),
                                           datetime.now(timezone.utc)).ready
        for obligation in world.ledger(assignment["id"]):
            settle(world, actor, obligation, evidence=[{"kind": "provider_request", "id": request_id}])
        mission = get(world.conn, "mission", assignment["mission_id"])
        execute(world.conn, actor, Command(operation="complete_mission", target_id=mission["id"],
            expected_revision=mission["revision"], args={"note": "Delivered the exact-head specialist assessment and receipt."}),
            world.service, world.scheduler)
        observe(world, grant, request_id, "request_completed", status="completed")
        finished = world.scheduler.finish(world.conn, world.host_actor,
            get(world.conn, "execution", grant["execution_id"]), "succeeded")
        assert finished["status"] == "completed"
        reviewed.append(assignment["id"])
        if len(reviewed) == 1:
            active.append(world.claim())
            assert active[-1] is not None and used(world, review) == 3
        if len(reviewed) < 4:
            assert not review_readiness(world.conn, item)["ready"]

    assert len(set(reviewed)) == 4 and used(world, review) == 0
    assert review_readiness(world.conn, item)["ready"]
    grant = world.claim()
    assert grant["assignment_id"] == str(integration["id"])
    actor = authenticate(world.conn, grant["execution_token"])
    decision = queue_review(world.conn, actor, world.service, world.scheduler, dict(
        forge_item_id=item["id"], commit_oid=item["head_commit_oid"], verdict="approved",
        summary="Accept the four independently completed contract assessments."), "integration-decision")
    deliver(world, {**review, "actor": actor}, identity, decision)
    gate = get(world.conn, "outbox_operation", decision["id"])["payload"]["gate_result"]
    assert gate["status"] == "accepted"
    assert get(world.conn, "review_gate", gate["review_gate_id"])["accepted_commit_oid"] == item["head_commit_oid"]
    assert get(world.conn, "assignment", review["assignment"]["id"])["status"] == "completed"
    assert all(get(world.conn, "mission", row["assignment"]["mission_id"])["status"] == "completed" for row in prepared)
