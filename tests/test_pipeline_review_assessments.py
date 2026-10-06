from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from archon_horizon.pipeline.connectors import ConnectorManager
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.models import ForgeReview
from archon_horizon.pipeline.records import change, create, get, snapshot
from archon_horizon.pipeline.schema import tables
from archon_horizon.pipeline.review_contracts import ReviewPlan
from archon_horizon.pipeline.reviewer_invocations import ReviewerReport, attach, prepare, read, report, ReviewerAttach
from archon_horizon.pipeline.reviews import postprocessing_review_panel, queue_merge, queue_review
from test_pipeline_reviewer_invocations import observed, review  # noqa: F401
from test_pipeline_reviewer_reports import reviewer_account
from test_pipeline_service import service_database, world  # noqa: F401


def assessment(**changes):
    return dict(scope="Metric API and its ordinary consumers", dimensions=["mathematical_contract", "public_design"],
        complete=True, classification_confirmed=True, evidence=["Fixed-metric consumer compiles at the reviewed head"], **changes)


def test_structured_assessment_renders_without_heading_protocol():
    parsed = ForgeReview(forge_item_id=uuid4(), commit_oid="a" * 40, verdict="approved", assessment=assessment())
    assert parsed.summary.startswith("**Approved.**")
    assert "Blocking findings" not in parsed.summary
    assert parsed.assessment.dimensions == ["mathematical_contract", "public_design"]


@pytest.mark.parametrize("changes", [{"complete": False}, {"findings": [dict(key="F1", dimension="public_design",
    severity="blocking", scope="Metric.lean", finding="Unnecessary flow assumption", evidence=["Fixed metric is excluded"],
    requested_change="Accept a connection")]}])
def test_approval_rejects_incomplete_or_blocking_assessment(changes):
    data = assessment()
    data.update(changes)
    with pytest.raises(ValidationError):
        ForgeReview(forge_item_id=uuid4(), commit_oid="a" * 40, verdict="approved", assessment=data)


def test_shadow_plan_cannot_activate_relaxed_coverage():
    with pytest.raises(ValidationError):
        ReviewPlan(mode="enforced", base_commit_oid="b" * 40, scope_paths=["Metric.lean"],
            questions=[dict(dimension="public_design", question="Can fixed metrics consume this?")])


def setup(world, review):
    identity = reviewer_account(world, review)
    item = change(world.conn, "forge_item", review["item"]["id"], review_phase="postprocessing")
    policy = change(world.conn, "review_policy", world.policy["id"], phases=["postprocessing"])
    return identity, item, policy


def objection(world, review, *, head="a", descriptor=None, verdict="changes_requested"):
    return create(world.conn, "forge_review", forge_item_id=review["item"]["id"], remote_id=str(uuid4()),
        reviewer_remote_id="specialist", reviewer_descriptor_id=descriptor or review["descriptor"]["id"],
        verdict=verdict, summary="The definition unnecessarily requires an entire Ricci flow.",
        commit_oid=head * 40, observed_at=datetime(2020, 1, 1, tzinfo=timezone.utc))


def invocation(world, review, *, plan=None):
    data = review["data"].model_copy(update={"review_plan": plan})
    result = prepare(world.conn, review["actor"], data, world.service)
    native_id = str(uuid4())
    attach(world.conn, review["actor"], result["provider_request_id"], ReviewerAttach(native_key=native_id))
    # These coverage tests publish an already finished review; Forge delivery
    # alone does not complete the provider invocation.
    observed(world, review, {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "wait",
        "agents_states": {native_id: {"status": "completed"}}}})
    return result


def deliver(world, review, identity, operation):
    item = get(world.conn, "forge_item", review["item"]["id"])
    manager = ConnectorManager(None, world.service, lambda _: {})
    reference = manager._record_review(world.conn, operation, dict(item=item, actor=review["actor"], identity=identity,
        repository=get(world.conn, "repository", item["repository_id"])),
        dict(id=str(uuid4()), user={"id": "specialist"}, state="APPROVED", commit_id=item["head_commit_oid"],
             _horizon_base_oid=operation["payload"].get("expected_base_oid")))
    change(world.conn, "outbox_operation", operation["id"], status="completed", result_ref_id=reference)
    return get(world.conn, "forge_review", get(world.conn, "object_reference", reference)["forge_review_id"])


def test_old_head_objection_requires_delivered_independent_explicit_resolution(world, review):
    identity, item, policy = setup(world, review)
    source = objection(world, review, head="b")
    prepared = invocation(world, review)
    result = report(world.conn, review["actor"], prepared["provider_request_id"], ReviewerReport(verdict="approved",
        assessment=assessment(resolutions=[dict(review_id=source["id"], disposition="repaired",
            explanation="Definition now uses a generic connection", evidence=["Fixed non-Ricci-flat metric consumer passes"])])),
        world.service, "resolution")
    assert postprocessing_review_panel(world.conn, item, policy)["unresolved_review_ids"] == [str(source["id"])]
    deliver(world, review, identity, result)
    panel = postprocessing_review_panel(world.conn, item, policy)
    assert panel["missing"] == []
    assert panel["unresolved_review_ids"] == []
    # A further push cannot inherit a repair confirmation without reinspection.
    item = change(world.conn, "forge_item", item["id"], head_commit_oid="c" * 40)
    assert postprocessing_review_panel(world.conn, item, policy)["unresolved_review_ids"] == [str(source["id"])]


def test_plain_approval_reports_missing_resolutions_before_delivery(world, review):
    identity, item, policy = setup(world, review)
    source = objection(world, review)
    prepared = invocation(world, review)
    with pytest.raises(DomainError) as error:
        report(world.conn, review["actor"], prepared["provider_request_id"],
            ReviewerReport(verdict="approved", assessment=assessment()), world.service, "approval")
    assert error.value.code == "review_resolutions_required"
    assert error.value.details["review_ids"] == [str(source["id"])]
    assert error.value.details["review_url_template"] == "/api/v3/records/forge_review/{id}"
    assert postprocessing_review_panel(world.conn, item, policy)["unresolved_review_ids"] == [str(source["id"])]
    assert not world.conn.execute(select(tables["outbox_operation"].c.id).where(
        tables["outbox_operation"].c.idempotency_key == "approval")).first()


def test_approval_requires_all_own_objections_but_not_another_reviewers(world, review):
    identity, item, _ = setup(world, review)
    first = objection(world, review, head="b")
    second = objection(world, review, head="c")
    other = create(world.conn, "reviewer_descriptor", project_id=world.project["id"], slug="other-reviewer",
        functions=["reviewer"], instructions="Review another dimension")
    objection(world, review, descriptor=other["id"])
    prepared = invocation(world, review)
    resolved = [dict(review_id=first["id"], disposition="repaired", explanation="Concrete definition repaired",
                     evidence=["Checked the concrete consumer at the pinned head"])]
    with pytest.raises(DomainError) as error:
        report(world.conn, review["actor"], prepared["provider_request_id"],
            ReviewerReport(verdict="approved", assessment=assessment(resolutions=resolved)), world.service, "partial")
    assert error.value.details["review_ids"] == [str(second["id"])]
    resolved.append({**resolved[0], "review_id": second["id"]})
    result = report(world.conn, review["actor"], prepared["provider_request_id"],
        ReviewerReport(verdict="approved", assessment=assessment(resolutions=resolved)), world.service, "all-own")
    assert result["payload"]["verdict"] == "approved"


@pytest.mark.parametrize("target", ["missing", "another_reviewer", "not_a_change_request"])
def test_invalid_resolution_identifies_bad_id_and_only_own_valid_targets(world, review, target):
    setup(world, review)
    source = objection(world, review)
    other = create(world.conn, "reviewer_descriptor", project_id=world.project["id"], slug="other-reviewer",
        functions=["reviewer"], instructions="Review another dimension")
    unrelated = objection(world, review, descriptor=other["id"])
    if target == "missing":
        invalid = uuid4()
    elif target == "another_reviewer":
        invalid = unrelated["id"]
    else:
        invalid = objection(world, review, head="b", verdict="commented")["id"]
    prepared = invocation(world, review)
    with pytest.raises(DomainError) as error:
        report(world.conn, review["actor"], prepared["provider_request_id"],
            ReviewerReport(verdict="approved", assessment=assessment(resolutions=[dict(
                review_id=invalid, disposition="repaired", explanation="The concrete defect was repaired",
                evidence=["Inspected the revised definition at the pinned head"])])),
            world.service, "invalid-target")
    assert error.value.code == "review_resolution_invalid"
    assert error.value.status == 422
    assert error.value.details["invalid_review_id"] == str(invalid)
    assert error.value.details["review_ids"] == [str(source["id"])]
    assert error.value.details["review_count"] == 1
    assert error.value.details["review_ids_truncated"] is False
    assert not world.conn.execute(select(tables["outbox_operation"].c.id).where(
        tables["outbox_operation"].c.idempotency_key == "invalid-target")).first()


def test_maintainer_cannot_waive_specialist_findings(world, review):
    setup(world, review)
    source = objection(world, review)
    with pytest.raises(DomainError, match="independent reviewer"):
        queue_review(world.conn, review["actor"], world.service, world.scheduler, dict(
            forge_item_id=review["item"]["id"], commit_oid="a" * 40, verdict="approved",
            assessment=assessment(resolutions=[dict(review_id=source["id"], disposition="withdrawn",
                explanation="I disagree", evidence=["Maintainer opinion"])])), "self-waiver")


@pytest.mark.parametrize("change_kind", ["policy", "rubric", "target"])
def test_confirmations_are_invalidated_by_protocol_or_target_changes(world, review, change_kind):
    identity, item, policy = setup(world, review)
    source = objection(world, review)
    prepared = invocation(world, review)
    result = report(world.conn, review["actor"], prepared["provider_request_id"], ReviewerReport(verdict="approved",
        assessment=assessment(resolutions=[dict(review_id=source["id"], disposition="repaired",
            explanation="Generalized connection input", evidence=["Consumer succeeds"])])), world.service, "repair")
    deliver(world, review, identity, result)
    if change_kind == "policy":
        policy = change(world.conn, "review_policy", policy["id"], instructions="Audit constructor reachability")
    elif change_kind == "rubric":
        change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], instructions="Audit constructor reachability")
    else:
        item = change(world.conn, "forge_item", item["id"], target_branch="new-main")
    assert postprocessing_review_panel(world.conn, item, policy)["unresolved_review_ids"] == [str(source["id"])]


def test_shadow_plan_is_pinned_and_requires_complete_classification(world, review):
    identity, item, policy = setup(world, review)
    plan = ReviewPlan(base_commit_oid="b" * 40, scope_paths=["Metric.lean"],
        questions=[dict(dimension="public_design", question="Can fixed metrics consume this?")])
    prepared = invocation(world, review, plan=plan)
    manifest = read(world.conn, world.actor, prepared["provider_request_id"], world.service)["manifest"]
    assert manifest["review_plan"] == plan.model_dump(mode="json")
    incomplete = assessment()
    incomplete["classification_confirmed"] = False
    with pytest.raises(DomainError, match="scope classification"):
        report(world.conn, review["actor"], prepared["provider_request_id"],
            ReviewerReport(verdict="approved", assessment=incomplete), world.service, "missing")
    result = report(world.conn, review["actor"], prepared["provider_request_id"],
        ReviewerReport(verdict="approved", assessment=assessment()), world.service, "scoped")
    assert result["payload"]["expected_base_oid"] == "b" * 40
    deliver(world, review, identity, result)
    panel = postprocessing_review_panel(world.conn, item, policy)
    assert panel["required"] == ["source-review"]
    assert panel["base_commit_oid"] == "b" * 40


def test_incomplete_current_review_supersedes_earlier_approval(world, review):
    identity, item, policy = setup(world, review)
    prepared = invocation(world, review)
    result = report(world.conn, review["actor"], prepared["provider_request_id"],
        ReviewerReport(verdict="approved", assessment=assessment()), world.service, "original")
    deliver(world, review, identity, result)
    create(world.conn, "forge_review", forge_item_id=item["id"], remote_id="incomplete", reviewer_remote_id="specialist",
        reviewer_descriptor_id=review["descriptor"]["id"], verdict="commented", summary="Consumer inspection unfinished",
        commit_oid=item["head_commit_oid"], observed_at=datetime.now(timezone.utc))
    assert "source-review" in postprocessing_review_panel(world.conn, item, policy)["missing"]


@pytest.mark.parametrize("phase", ["preprocessing", "formalization", "postprocessing"])
def test_adverse_review_blocks_merge_in_every_phase_and_invalidates_gate(world, review, phase):
    _, item, policy = setup(world, review)
    item = change(world.conn, "forge_item", item["id"], review_phase=phase)
    policy = change(world.conn, "review_policy", policy["id"], phases=[phase])
    approval = create(world.conn, "forge_review", forge_item_id=item["id"], remote_id="maintainer",
        reviewer_remote_id="maintainer", verdict="approved", summary="Accept", commit_oid=item["head_commit_oid"],
        observed_at=datetime.now(timezone.utc))
    gate = create(world.conn, "review_gate", forge_item_id=item["id"], policy_id=policy["id"],
        policy_revision_id=snapshot(world.conn, "review_policy", policy, world.actor.id), status="accepted",
        accepted_commit_oid=item["head_commit_oid"], maintainer_review_id=approval["id"], evaluated_at=datetime.now(timezone.utc))
    source = objection(world, review, head="b")
    with pytest.raises(DomainError, match="specialist coverage"):
        queue_merge(world.conn, review["actor"], dict(forge_item_id=item["id"], review_gate_id=gate["id"],
            expected_head_oid=item["head_commit_oid"]), "blocked-merge")
    ConnectorManager._activate_gate(world.conn, {"payload": {"reviewer_descriptor_id": str(review["descriptor"]["id"])}},
        dict(item=item, actor=review["actor"], repository=get(world.conn, "repository", item["repository_id"])), source)
    assert get(world.conn, "review_gate", gate["id"])["status"] == "pending"


def test_other_reviewer_cannot_withdraw_an_objection(world, review):
    setup(world, review)
    other = create(world.conn, "reviewer_descriptor", project_id=world.project["id"], slug="other-reviewer",
        functions=["reviewer"], instructions="Review APIs")
    source = objection(world, review, descriptor=other["id"])
    prepared = invocation(world, review)
    with pytest.raises(DomainError, match="by this reviewer"):
        report(world.conn, review["actor"], prepared["provider_request_id"], ReviewerReport(verdict="approved",
            assessment=assessment(resolutions=[dict(review_id=source["id"], disposition="withdrawn",
                explanation="Different rubric disagrees", evidence=["Opinion"])])), world.service, "wrong-reviewer")


def test_explicit_validated_carry_retains_confirmed_resolution(world, review):
    identity, item, policy = setup(world, review)
    source = objection(world, review, head="b")
    prepared = invocation(world, review)
    result = report(world.conn, review["actor"], prepared["provider_request_id"], ReviewerReport(verdict="approved",
        assessment=assessment(resolutions=[dict(review_id=source["id"], disposition="repaired",
            explanation="Generic connection replaces flow requirement", evidence=["Fixed-metric consumer succeeds"])])),
        world.service, "confirmed")
    confirmed = deliver(world, review, identity, result)
    item = change(world.conn, "forge_item", item["id"], head_commit_oid="c" * 40)
    evidence = dict(kind="review_carry_forward", version=1, forge_item_id=str(item["id"]),
        source_review_id=str(confirmed["id"]), source_commit_oid="a" * 40, head_commit_oid="c" * 40,
        target_branch="main", base_commit_oid="d" * 40, scope_paths=["Metric.lean"],
        delta_analysis="Only an unrelated private proof changed", dependency_analysis="The definition and its closure are unchanged",
        rationale="Confirmed generality fix and consumer remain applicable")
    artifact = create(world.conn, "artifact", project_id=world.project["id"], kind="blob",
        content=world.service.store.put_json(evidence))
    operation = queue_review(world.conn, review["actor"], world.service, world.scheduler, dict(
        forge_item_id=item["id"], commit_oid="c" * 40, verdict="approved", summary="The unchanged API retains its independent assessment",
        carry_forward=[dict(source_review_id=confirmed["id"], evidence_artifact_id=artifact["id"])]), "carry-confirmation")
    manager = ConnectorManager(None, world.service, lambda _: {})
    reference = manager._record_review(world.conn, operation, dict(item=item, actor=review["actor"],
        repository=get(world.conn, "repository", item["repository_id"])),
        dict(id=str(uuid4()), user={"id": "maintainer"}, state="APPROVED", commit_id="c" * 40, _horizon_base_oid="d" * 40))
    change(world.conn, "outbox_operation", operation["id"], status="completed", result_ref_id=reference)
    panel = postprocessing_review_panel(world.conn, item, policy)
    assert panel["missing"] == []
    assert panel["carried_forward"] == ["source-review"]
    assert panel["base_commit_oid"] == "d" * 40
