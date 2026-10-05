from uuid import uuid4

import pytest
from sqlalchemy import select, update

from archon_horizon.pipeline.connectors import ConnectorManager
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.records import change, create, get, snapshot
from archon_horizon.pipeline.reviewer_invocations import ReviewerCancel, ReviewerPrepare, ReviewerReport, cancel, prepare, prepare_assignment, read, report
from archon_horizon.pipeline.reviews import queue_review, review_readiness
from archon_horizon.pipeline.schema import tables
from test_pipeline_review_assessments import assessment, deliver, invocation, objection, setup
from test_pipeline_reviewer_invocations import review  # noqa: F401
from test_pipeline_reviewer_reports import reviewer_account
from test_pipeline_service import service_database, world  # noqa: F401


def final_approval(world, review):
    return queue_review(world.conn, review["actor"], world.service, world.scheduler, dict(
        forge_item_id=review["item"]["id"], commit_oid="a" * 40,
        verdict="approved", summary="Accept the independently reviewed change."), str(uuid4()))


def test_reviewer_ownership_tracks_an_imported_pr_in_run_coordination(world, review):
    from archon_horizon.pipeline.coordination_memory import frontier, memory

    item = create(world.conn, "forge_item", repository_id=review["item"]["repository_id"],
        remote_number=2, kind="pull_request", review_phase="preprocessing", target_branch="main",
        title="Imported contract", status="open", head_commit_oid="a" * 40,
        observed_at=review["item"]["observed_at"])
    initial = frontier(world.conn, review["run"]["id"])
    prepare(world.conn, review["actor"], review["data"].model_copy(update={"forge_item_id": item["id"]}), world.service)
    owned = frontier(world.conn, review["run"]["id"])
    assert owned != initial
    assessment = create(world.conn, "forge_review", forge_item_id=item["id"],
        remote_id="external-review", reviewer_remote_id="source-review", verdict="changes_requested",
        summary="Definition needs clarification", commit_oid="a" * 40,
        observed_at=review["item"]["observed_at"])
    assert frontier(world.conn, review["run"]["id"]) != owned
    assert [row["id"] for row in memory(world.conn, review["run"]["id"])["current_head_reviews"]] == [assessment["id"]]


def test_direct_rubric_cannot_publish_green_approval_or_satisfy_gate(world, review):
    identity, item, policy = setup(world, review)
    operation = queue_review(world.conn, review["actor"], world.service, world.scheduler, dict(
        forge_item_id=item["id"], commit_oid=item["head_commit_oid"], verdict="approved",
        reviewer_descriptor_id=review["descriptor"]["id"], summary="Positive maintainer rubric assessment."), "direct")
    assert operation["payload"]["verdict"] == "commented"
    assert operation["payload"]["rubric_verdict"] == "approved"
    # Even a recovered legacy remote APPROVED response must not produce /ok.
    delivered = deliver(world, review, identity, operation)
    labels = world.conn.execute(select(tables["outbox_operation"]).where(
        tables["outbox_operation"].c.kind == "forge_label")).mappings().all()
    assert any("review/source-review/commented" in row["payload"]["add"] for row in labels)
    assert not any("review/source-review/ok" in row["payload"]["add"] for row in labels)
    readiness = review_readiness(world.conn, item, policy)
    assert readiness["missing_dimensions"] == ["source-review"]
    assert readiness["invalid_provenance"][0]["review_id"] == str(delivered["id"])
    assert readiness["next_actions"][0]["reviewer_descriptor_id"] == str(review["descriptor"]["id"])
    before = world.conn.execute(select(tables["outbox_operation"].c.id)).all()
    with pytest.raises(DomainError) as error:
        final_approval(world, review)
    assert error.value.code == "review_gate_blocked"
    assert error.value.details["review_readiness"]["ready"] is False
    assert world.conn.execute(select(tables["outbox_operation"].c.id)).all() == before
    assert not world.conn.execute(select(tables["review_gate"])).first()


def test_completed_native_review_enables_final_gate_and_green_label(world, review):
    identity, item, policy = setup(world, review)
    prepared = invocation(world, review)
    operation = report(world.conn, review["actor"], prepared["provider_request_id"],
        ReviewerReport(verdict="approved", assessment=assessment()), world.service, "native")
    deliver(world, review, identity, operation)
    readiness = review_readiness(world.conn, item, policy)
    assert readiness["ready"] is True
    assert readiness["reviewed_dimensions"] == ["source-review"]
    assert readiness["invalid_provenance"] == []
    label = world.conn.execute(select(tables["outbox_operation"]).where(
        tables["outbox_operation"].c.idempotency_key == "review-label:" + str(operation["id"]))).mappings().one()
    assert label["payload"]["add"] == ["review/source-review/ok"]
    final = final_approval(world, review)
    deliver(world, review, identity, final)
    result = get(world.conn, "outbox_operation", final["id"])["payload"]["gate_result"]
    assert result["status"] == "accepted"
    assert get(world.conn, "review_gate", result["review_gate_id"])["accepted_commit_oid"] == item["head_commit_oid"]


def test_contract_readiness_explains_missing_route_and_invalid_rubric_provenance(world, review):
    change(world.conn, "project", world.project["id"], workflow="milestones")
    descriptor = change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], slug="definitions")
    item = review["item"]
    create(world.conn, "milestone_check", project_id=world.project["id"], repository_id=item["repository_id"],
        principal_id=world.actor.id, source_commit_oid=item["head_commit_oid"], base_commit_oid="b" * 40,
        kind="contract", report={"ancestor_commits": ["b" * 40], "milestone_keys": ["M01"]}, report_sha256="c" * 64)
    operation = queue_review(world.conn, review["actor"], world.service, world.scheduler, dict(
        forge_item_id=item["id"], commit_oid=item["head_commit_oid"], verdict="approved",
        reviewer_descriptor_id=descriptor["id"], summary="Direct rubric assessment."), "contract-direct")
    deliver(world, review, {"id": None, "remote_user_id": "specialist"}, operation)
    readiness = review_readiness(world.conn, item)
    assert set(readiness["missing_dimensions"]) == {"statement-fidelity", "definitions", "decomposition", "library-api"}
    assert "missing_accepted_route" in {row["code"] for row in readiness["blockers"]}
    assert readiness["invalid_provenance"][0]["dimension"] == "definitions"
    assert readiness["invalid_provenance"][0]["code"] == "review_carry_invalid"
    assert readiness["next_actions"][0]["reviewer_descriptor_id"] == str(descriptor["id"])
    with pytest.raises(DomainError) as error:
        final_approval(world, review)
    assert error.value.code == "review_gate_blocked"


@pytest.mark.parametrize("changed", ["target", "policy"])
def test_native_milestone_coverage_requires_current_delivery_pins(world, review, changed):
    identity = reviewer_account(world, review)
    change(world.conn, "project", world.project["id"], workflow="milestones")
    change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], slug="decomposition")
    item = review["item"]
    create(world.conn, "milestone_check", project_id=world.project["id"], repository_id=item["repository_id"],
        principal_id=world.actor.id, source_commit_oid=item["head_commit_oid"], base_commit_oid="b" * 40,
        kind="route", report={"ancestor_commits": ["b" * 40], "milestone_keys": ["M01"]}, report_sha256="c" * 64)
    prepared = invocation(world, review)
    native = report(world.conn, review["actor"], prepared["provider_request_id"],
        ReviewerReport(verdict="approved", assessment=assessment()), world.service, "native-route")
    deliver(world, review, identity, native)
    assert review_readiness(world.conn, item)["ready"] is True
    if changed == "target":
        item = change(world.conn, "forge_item", item["id"], target_branch="other")
    else:
        change(world.conn, "review_policy", world.policy["id"], instructions="A new review policy")
    readiness = review_readiness(world.conn, item)
    assert readiness["missing_dimensions"] == ["decomposition"]
    assert readiness["invalid_provenance"][0]["code"] == "invalid_specialist_provenance"
    with pytest.raises(DomainError) as error:
        final_approval(world, review)
    assert error.value.code == "review_gate_blocked"


@pytest.mark.parametrize("mutation,code", [("head", "review_head_changed"),
    ("policy", "review_policy_pin_stale"), ("pin", "review_policy_pin_missing")])
def test_delivered_approval_retains_diagnostic_when_gate_is_blocked(world, review, mutation, code):
    identity, item, policy = setup(world, review)
    prepared = invocation(world, review)
    native = report(world.conn, review["actor"], prepared["provider_request_id"],
        ReviewerReport(verdict="approved", assessment=assessment()), world.service, "native")
    deliver(world, review, identity, native)
    operation = final_approval(world, review)
    if mutation == "head":
        change(world.conn, "forge_item", item["id"], head_commit_oid="b" * 40)
    elif mutation == "policy":
        change(world.conn, "review_policy", policy["id"], instructions="A revised policy")
    else:
        payload = dict(operation["payload"])
        del payload["policy_revision_id"]
        operation = change(world.conn, "outbox_operation", operation["id"], payload=payload)
    manager = ConnectorManager(None, world.service, lambda _: {})
    reference = manager._record_review(world.conn, operation, dict(item=item, actor=review["actor"], identity=identity,
        repository=get(world.conn, "repository", item["repository_id"])), dict(id=str(uuid4()),
        user={"id": "specialist"}, state="APPROVED", commit_id="a" * 40))
    change(world.conn, "outbox_operation", operation["id"], status="completed", result_ref_id=reference)
    receipt = get(world.conn, "outbox_operation", operation["id"])
    assert receipt["status"] == "completed"
    result = receipt["payload"]["gate_result"]
    assert result["status"] == "blocked" and result["remote_review_delivered"] is True
    assert code in {row["code"] for row in result["review_readiness"]["blockers"]}
    assert not world.conn.execute(select(tables["review_gate"])).first()


@pytest.mark.parametrize("status", ["pending", "submitted", "running", "uncertain"])
def test_readiness_collects_existing_owner_instead_of_preparing_duplicates(world, review, status):
    _, item, policy = setup(world, review)
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    change(world.conn, "provider_request", prepared["provider_request_id"], status=status)
    action = review_readiness(world.conn, item, policy)["next_actions"][0]
    assert action["action"] == "collect_existing_reviewer_invocation"
    assert action["method"] == "GET"
    assert action["provider_request_id"] == str(prepared["provider_request_id"])
    assert action["owner_execution_id"] == str(review["execution"]["id"])
    assert action["owner_status"] == status
    assert "retry_of" not in action
    assert "schema_lookup" not in action and "schema_command" not in action
    assert "request_template" not in action


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_readiness_supplies_retry_contract_accepted_by_prepare(world, review, status):
    _, item, policy = setup(world, review)
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    cancel(world.conn, review["actor"], prepared["provider_request_id"], ReviewerCancel(note="Native launch unavailable"))
    change(world.conn, "provider_request", prepared["provider_request_id"], status=status)
    action = review_readiness(world.conn, item, policy)["next_actions"][0]
    assert action["action"] == "retry_reviewer_invocation"
    assert action["retry_of"] == str(prepared["provider_request_id"])
    assert action["owner_status"] == status
    assert action["retry_note_required"] is True and "retry_note" in action["required_fields"]
    assert action["schema_lookup"] == {"section": "operations", "name": "prepare_reviewer"}
    assert action["schema_command"] == "horizon-pipeline agent schema --section operations --name prepare_reviewer"
    assert set(action["request_template"]) <= set(ReviewerPrepare.model_fields)
    assert action["request_template"]["retry_of"] == action["retry_of"]
    data = {**action["request_template"], "parent_request_id": review["request"]["id"],
            "retry_note": "Enabled the missing native launch feature and verified available tools"}
    retried = prepare(world.conn, review["actor"], ReviewerPrepare.model_validate(data), world.service)
    assert retried["provider_request_id"] != prepared["provider_request_id"]
    next_action = review_readiness(world.conn, item, policy)["next_actions"][0]
    assert next_action["action"] == "collect_existing_reviewer_invocation"
    assert next_action["attempts"] == 2


@pytest.mark.parametrize("attempts", [1, 5])
def test_readiness_blocks_retry_until_provider_reservations_are_reconciled(world, review, attempts):
    _, item, policy = setup(world, review)
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    work = tables["review_work"]
    world.conn.execute(update(work).where(work.c.provider_request_id == prepared["provider_request_id"]).values(attempts=attempts))
    change(world.conn, "provider_request", prepared["provider_request_id"], status="interrupted")
    readiness = review_readiness(world.conn, item, policy)
    action = readiness["next_actions"][0]
    assert action["action"] == "reconcile_reviewer_owner"
    assert "retry_of" not in action
    assert "review_unsettled" in {blocker["code"] for blocker in readiness["blockers"]}
    data = review["data"].model_copy(update={"retry_of": prepared["provider_request_id"], "retry_note": "Retry after failure"})
    with pytest.raises(DomainError) as error:
        prepare(world.conn, review["actor"], data, world.service)
    assert error.value.code == "review_unsettled"


@pytest.mark.parametrize("attempts", [3, 7])
def test_readiness_allows_explicit_repaired_retry_after_many_attempts(world, review, attempts):
    _, item, policy = setup(world, review)
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    cancel(world.conn, review["actor"], prepared["provider_request_id"], ReviewerCancel(note="Native launch failed"))
    work = tables["review_work"]
    world.conn.execute(update(work).where(work.c.provider_request_id == prepared["provider_request_id"]).values(attempts=attempts))
    readiness = review_readiness(world.conn, item, policy)
    action = readiness["next_actions"][0]
    assert action["action"] == "retry_reviewer_invocation" and action["attempts"] == attempts
    assert action["retry_of"] == str(prepared["provider_request_id"])
    assert action["retry_note_required"] is True
    assert readiness["ready"] is False
    assert "review_retry_exhausted" not in {blocker["code"] for blocker in readiness["blockers"]}
    duplicate = prepare(world.conn, review["actor"], review["data"], world.service)
    assert duplicate["provider_request_id"] == prepared["provider_request_id"]
    retried = prepare(world.conn, review["actor"], ReviewerPrepare.model_validate({
        **action["request_template"], "parent_request_id": review["request"]["id"],
        "retry_note": "Fixed provider event correlation and confirmed the old native child is stopped",
    }), world.service)
    assert retried["provider_request_id"] != prepared["provider_request_id"]
    assert review_readiness(world.conn, item, policy)["next_actions"][0]["attempts"] == attempts + 1


@pytest.mark.parametrize("changed", ["head", "target", "policy", "descriptor"])
def test_readiness_starts_new_contract_without_retrying_old_owner(world, review, changed):
    _, item, policy = setup(world, review)
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    cancel(world.conn, review["actor"], prepared["provider_request_id"], ReviewerCancel(note="Native launch failed"))
    if changed == "head":
        item = change(world.conn, "forge_item", item["id"], head_commit_oid="c" * 40)
    elif changed == "target":
        item = change(world.conn, "forge_item", item["id"], target_branch="other")
    elif changed == "policy":
        policy = change(world.conn, "review_policy", policy["id"], instructions="Revised policy")
    else:
        change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], instructions="Revised reviewer scope")
    action = review_readiness(world.conn, item, policy)["next_actions"][0]
    assert action["action"] == "prepare_reviewer_invocation"
    assert action["schema_lookup"] == {"section": "operations", "name": "prepare_reviewer"}
    assert set(action["request_template"]) == {"forge_item_id", "reviewer_descriptor_id", "expected_head_oid"}
    assert set(action["request_template"]) <= set(ReviewerPrepare.model_fields)
    assert "retry_of" not in action and "owner_id" not in action


def test_completed_missing_report_requires_physical_settlement_before_retry(world, review):
    _, item, policy = setup(world, review)
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    change(world.conn, "provider_request", prepared["provider_request_id"], status="completed")
    readiness = review_readiness(world.conn, item, policy)
    assert readiness["next_actions"][0]["action"] == "reconcile_reviewer_owner"
    assert "retry_of" not in readiness["next_actions"][0]
    assert "review_unsettled" in {blocker["code"] for blocker in readiness["blockers"]}
    world.conn.execute(update(tables["resource_claim"]).where(
        tables["resource_claim"].c.provider_request_id == prepared["provider_request_id"]).values(
            released_at=review["item"]["observed_at"]))
    action = review_readiness(world.conn, item, policy)["next_actions"][0]
    assert action["action"] == "retry_reviewer_invocation"
    assert action["report_defect"]["code"] == "review_report_missing"
    retried = prepare(world.conn, review["actor"], ReviewerPrepare.model_validate({
        **action["request_template"], "parent_request_id": review["request"]["id"],
        "retry_note": "The prior stopped reviewer omitted its report; publish the missing assessment",
    }), world.service)
    assert retried["provider_request_id"] != prepared["provider_request_id"]


@pytest.mark.parametrize("same_invocation", [False, True])
def test_completed_approval_missing_old_resolutions_can_be_remediated_with_same_pins(world, review, same_invocation):
    from datetime import datetime, timezone
    from archon_horizon.pipeline.review_contracts import ReviewPlan

    identity, item, policy = setup(world, review)
    plan = ReviewPlan(base_commit_oid="b" * 40, scope_paths=["Metric.lean"], questions=[
        {"dimension": "public_design", "question": "Can the fixed metric consumer use this API?"}])
    prepared = invocation(world, review, plan=plan)
    operation = report(world.conn, review["actor"], prepared["provider_request_id"],
        ReviewerReport(verdict="approved", assessment=assessment()), world.service, "legacy-approval")
    delivered = deliver(world, review, identity, operation)
    # Model a legacy approval delivered before explicit-resolution enforcement.
    previous = create(world.conn, "forge_review", forge_item_id=item["id"], remote_id="old-objection",
        reviewer_remote_id="specialist", reviewer_descriptor_id=review["descriptor"]["id"],
        provider_request_id=prepared["provider_request_id"] if same_invocation else None,
        verdict="changes_requested", summary="The endpoint bridge needs an explicit hypothesis",
        commit_oid=("a" if same_invocation else "b") * 40,
        observed_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    readiness = review_readiness(world.conn, item, policy)
    assert readiness["missing_dimensions"] == []
    assert readiness["ready"] is False
    action = next(row for row in readiness["next_actions"] if row.get("retry_of"))
    assert action["action"] == "retry_reviewer_invocation"
    assert action["report_defect"]["code"] == "review_resolutions_required"
    assert action["report_defect"]["review_ids"] == [str(previous["id"])]
    data = ReviewerPrepare.model_validate({**action["request_template"],
        "parent_request_id": review["request"]["id"],
        "retry_note": "Inspect and explicitly account for the prior objections in the typed report. "
            + "Additional preserved diagnostic context. " * 40 + "END_OF_FULL_RETRY_NOTE"})
    changed_plan = plan.model_copy(update={"base_commit_oid": "c" * 40})
    with pytest.raises(DomainError) as error:
        prepare(world.conn, review["actor"], data.model_copy(update={"review_plan": changed_plan}), world.service)
    assert error.value.code == "review_retry_changed"
    retried = prepare(world.conn, review["actor"], data, world.service)
    manifest = read(world.conn, world.actor, retried["provider_request_id"], world.service)["manifest"]
    assert manifest["review_plan"] == plan.model_dump(mode="json")
    assert manifest["retry_of"] == str(prepared["provider_request_id"])
    assert manifest["retry_note"] == data.retry_note
    assert "explicitly account for the prior objections" in manifest["prompt"]
    assert "END_OF_FULL_RETRY_NOTE" not in manifest["prompt"]
    assert "/manifest/retry_note" in manifest["prompt"]
    assert "recovery request does not establish approval" in manifest["prompt"]
    assert manifest["head_commit_oid"] == item["head_commit_oid"]
    assert manifest["reviewer_descriptor_id"] == str(review["descriptor"]["id"])
    assert get(world.conn, "forge_review", delivered["id"])["verdict"] == "approved"
    assert get(world.conn, "provider_request", prepared["provider_request_id"])["status"] == "completed"
    with pytest.raises(DomainError) as error:
        prepare(world.conn, review["actor"], data, world.service)
    assert error.value.code == "revision_conflict"


@pytest.mark.parametrize("verdict", ["approved", "changes_requested"])
def test_completed_substantive_review_cannot_be_retried_for_another_verdict(world, review, verdict):
    identity, item, policy = setup(world, review)
    prepared = invocation(world, review)
    if verdict == "approved":
        operation = report(world.conn, review["actor"], prepared["provider_request_id"],
            ReviewerReport(verdict="approved", assessment=assessment()), world.service, "valid-approval")
        deliver(world, review, identity, operation)
    else:
        create(world.conn, "forge_review", forge_item_id=item["id"], remote_id="substantive-finding",
            reviewer_remote_id="specialist", reviewer_descriptor_id=review["descriptor"]["id"],
            provider_request_id=prepared["provider_request_id"], verdict=verdict,
            summary="The theorem omits a required hypothesis; repair the source", commit_oid=item["head_commit_oid"],
            observed_at=item["observed_at"])
    with pytest.raises(DomainError) as error:
        prepare(world.conn, review["actor"], review["data"].model_copy(update={
            "retry_of": prepared["provider_request_id"], "retry_note": "Try for another opinion"}), world.service)
    assert error.value.code == "review_result_not_retryable"
    if verdict == "changes_requested":
        action = review_readiness(world.conn, item, policy)["next_actions"][0]
        assert action["action"] == "repair_existing_reviewer_result" and "retry_of" not in action


@pytest.mark.parametrize("defect", ["commented", "provenance", "delivery"])
def test_completed_report_defects_are_distinguished_from_unsettled_delivery(world, review, defect):
    identity, item, policy = setup(world, review)
    prepared = invocation(world, review)
    if defect == "delivery":
        report(world.conn, review["actor"], prepared["provider_request_id"],
            ReviewerReport(verdict="approved", assessment=assessment()), world.service, "pending-report")
    else:
        create(world.conn, "forge_review", forge_item_id=item["id"], remote_id="ineligible-report",
            reviewer_remote_id="specialist", reviewer_descriptor_id=review["descriptor"]["id"],
            provider_request_id=prepared["provider_request_id"],
            verdict="commented" if defect == "commented" else "approved",
            summary="Report requires completion", commit_oid=item["head_commit_oid"], observed_at=item["observed_at"])
    action = review_readiness(world.conn, item, policy)["next_actions"][0]
    data = review["data"].model_copy(update={"retry_of": prepared["provider_request_id"],
        "retry_note": "Repair the diagnosed incomplete report with independent evidence"})
    if defect == "delivery":
        assert action["action"] == "repair_existing_reviewer_result"
        with pytest.raises(DomainError) as error:
            prepare(world.conn, review["actor"], data, world.service)
        assert error.value.details["report_defect"]["code"] == "review_delivery_unsettled"
    else:
        assert action["action"] == "retry_reviewer_invocation"
        assert prepare(world.conn, review["actor"], data, world.service)["provider_request_id"] != prepared["provider_request_id"]


def test_maintainer_rubric_after_completed_objection_does_not_unlock_retry(world, review):
    identity, item, _ = setup(world, review)
    prepared = invocation(world, review)
    create(world.conn, "forge_review", forge_item_id=item["id"], remote_id="independent-negative",
        reviewer_remote_id="specialist", reviewer_descriptor_id=review["descriptor"]["id"],
        provider_request_id=prepared["provider_request_id"], verdict="changes_requested",
        summary="Repair the missing hypothesis", commit_oid=item["head_commit_oid"],
        observed_at=item["observed_at"])
    operation = queue_review(world.conn, review["actor"], world.service, world.scheduler, dict(
        forge_item_id=item["id"], commit_oid=item["head_commit_oid"], verdict="approved",
        reviewer_descriptor_id=review["descriptor"]["id"], provider_request_id=prepared["provider_request_id"],
        summary="Maintainer disagrees with the independent negative"), "raw-rubric-after-objection")
    deliver(world, review, identity, operation)
    with pytest.raises(DomainError) as error:
        prepare(world.conn, review["actor"], review["data"].model_copy(update={
            "retry_of": prepared["provider_request_id"], "retry_note": "Seek another verdict"}), world.service)
    assert error.value.code == "review_result_not_retryable"


def test_completed_durable_report_repair_preserves_history_and_requires_stopped_execution(world, review):
    from datetime import datetime, timezone
    from archon_horizon.pipeline.auth import authenticate
    from test_pipeline_durable_review_lifecycle import observe

    identity, item, policy = setup(world, review)
    change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], invocation="assignment")
    world.conn.execute(update(tables["host_harness"]).values(execution_slots=2))
    prepared = prepare_assignment(world.conn, review["actor"], review["data"], world.service)
    owner = prepared["assignment"]
    grant = world.claim()
    assert grant["assignment_id"] == str(owner["id"])
    actor = authenticate(world.conn, grant["execution_token"])
    request_id = uuid4()
    observe(world, grant, request_id, "request_started", goal="Assess the pinned contract",
        mission_revision_id=grant["mission_revision_id"], mission_revision_number=grant["mission_revision_number"],
        run_revision=grant["run_revision"])
    operation = report(world.conn, actor, request_id, ReviewerReport(verdict="approved", assessment=assessment()),
        world.service, "durable-legacy-approval")
    delivered = deliver(world, {**review, "actor": actor}, identity, operation)
    prior = objection(world, review, head="b")
    observe(world, grant, request_id, "request_completed", status="completed")
    change(world.conn, "assignment", owner["id"], status="completed")
    action = review_readiness(world.conn, item, policy)["next_actions"][0]
    assert action["action"] == "reconcile_reviewer_owner" and "retry_of" not in action
    data = review["data"].model_copy(update={"retry_of": owner["id"],
        "retry_note": "Complete the omitted prior-objection resolutions under the same independent rubric"})
    with pytest.raises(DomainError) as error:
        prepare_assignment(world.conn, review["actor"], data, world.service)
    assert error.value.code == "review_unsettled"
    now = datetime.now(timezone.utc)
    change(world.conn, "execution", grant["execution_id"], status="succeeded", finished_at=now, stop_confirmed_at=now)
    action = review_readiness(world.conn, item, policy)["next_actions"][0]
    assert action["action"] == "retry_reviewer_assignment"
    assert action["retry_of"] == str(owner["id"])
    assert action["report_defect"]["review_ids"] == [str(prior["id"])]
    retried = prepare_assignment(world.conn, review["actor"], data, world.service)
    assert retried["assignment"]["id"] != owner["id"]
    assert retried["assignment"]["reviewer_descriptor_id"] == owner["reviewer_descriptor_id"]
    assert get(world.conn, "assignment", owner["id"])["status"] == "completed"
    assert get(world.conn, "forge_review", delivered["id"])["verdict"] == "approved"
    with pytest.raises(DomainError) as error:
        prepare_assignment(world.conn, review["actor"], data, world.service)
    assert error.value.code == "revision_conflict"


def test_readiness_preserves_standalone_assignment_ownership_and_retry_contract(world, review):
    _, item, policy = setup(world, review)
    change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], invocation="assignment")
    fresh = review_readiness(world.conn, item, policy)["next_actions"][0]
    assert fresh["action"] == "prepare_reviewer_assignment" and fresh["endpoint"] == "/api/v3/reviewer-assignments"
    assert fresh["schema_lookup"] == {"section": "operations", "name": "prepare_reviewer_assignment"}
    assert set(fresh["request_template"]) <= set(ReviewerPrepare.model_fields)
    ReviewerPrepare.model_validate({**fresh["request_template"], "parent_request_id": review["request"]["id"]})
    prepared = prepare_assignment(world.conn, review["actor"], review["data"], world.service)
    assignment = prepared["assignment"]
    work = tables["review_work"]
    world.conn.execute(update(work).where(work.c.assignment_id == assignment["id"]).values(attempts=5))
    action = review_readiness(world.conn, item, policy)["next_actions"][0]
    assert action["action"] == "collect_existing_reviewer_assignment"
    assert action["assignment_id"] == str(assignment["id"])
    change(world.conn, "assignment", assignment["id"], status="failed")
    action = review_readiness(world.conn, item, policy)["next_actions"][0]
    assert action["action"] == "retry_reviewer_assignment"
    assert action["retry_of"] == str(assignment["id"])
    assert action["attempts"] == 5
    # An unconfirmed physical execution fences this otherwise terminal owner.
    values = {key: value for key, value in review["execution"].items()
              if key not in {"id", "created_at", "updated_at", "revision"}}
    values.update(assignment_id=assignment["id"], number=1, status="failed",
        assignment_revision_id=snapshot(world.conn, "assignment", get(world.conn, "assignment", assignment["id"]), world.actor.id),
        mission_revision_id=snapshot(world.conn, "mission", get(world.conn, "mission", assignment["mission_id"]), world.actor.id))
    create(world.conn, "execution", **values)
    readiness = review_readiness(world.conn, item, policy)
    assert readiness["next_actions"][0]["action"] == "reconcile_reviewer_owner"
    assert "review_unsettled" in {blocker["code"] for blocker in readiness["blockers"]}
