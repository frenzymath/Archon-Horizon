import pytest
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import delete, select

from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.auth import authenticate
from archon_horizon.pipeline.records import change, create, get
from archon_horizon.pipeline.reviewer_invocations import ReviewerAttach, ReviewerReport, attach, prepare, report
from archon_horizon.pipeline.reviewer_invocations import ReviewerCancel, cancel
from archon_horizon.pipeline.review_labels import terminal_label_operation, terminal_state
from archon_horizon.pipeline.reviews import postprocessing_review_panel, validate_postprocessing_report
from archon_horizon.pipeline.schema import tables
from test_pipeline_reviewer_invocations import review  # noqa: F401
from test_pipeline_service import service_database, world  # noqa: F401


@pytest.mark.parametrize("role", ["maintainer", "worker"])
def test_pr_discussion_uses_maintainer_account_only_for_maintainer(world, review, role):
    from archon_horizon.pipeline.models import ForgeComment
    from archon_horizon.pipeline.reviews import queue_comment
    repository = get(world.conn, "repository", review["item"]["repository_id"])
    identity = create(world.conn, "integration_identity", integration_id=repository["integration_id"],
        principal_id=world.actor.id, remote_user_id="maintainer", credential_ref="secret:maintainer")
    change(world.conn, "review_policy", world.policy["id"], maintainer_identity_id=identity["id"])
    change(world.conn, "assignment", review["assignment"]["id"], role=role)
    operation = queue_comment(world.conn, review["actor"], ForgeComment(forge_item_id=review["item"]["id"],
        body="Review intent: inspect statement fidelity and reusable definitions."), world.scheduler, "intent")
    assert operation["payload"].get("integration_identity_id") == (str(identity["id"]) if role == "maintainer" else None)


def reviewer_account(world, review):
    repo = get(world.conn, "repository", review["item"]["repository_id"])
    identity = create(world.conn, "integration_identity", integration_id=repo["integration_id"],
        principal_id=world.actor.id, remote_user_id="specialist", credential_ref="secret:specialist")
    change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], integration_identity_id=identity["id"])
    return identity


@pytest.mark.parametrize("role", ["maintainer", "worker"])
def test_reviewer_publishes_before_native_exit_without_changing_lifecycle(world, review, role):
    identity = reviewer_account(world, review)
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    attach(world.conn, review["actor"], prepared["provider_request_id"], ReviewerAttach(native_key="review-child"))
    change(world.conn, "assignment", review["assignment"]["id"], role=role)
    result = report(world.conn, review["actor"], prepared["provider_request_id"],
        ReviewerReport(verdict="approved", summary="Statement and definitions checked within the declared scope."),
        world.service, "own-assessment")
    assert result["payload"]["integration_identity_id"] == str(identity["id"])
    assert result["payload"]["reviewer_submission"] is True
    assert get(world.conn, "provider_request", prepared["provider_request_id"])["status"] == "submitted"
    assert not world.conn.execute(select(tables["review_gate"])).first()


def test_terminal_forge_items_get_idempotent_outcome_labels(world, review):
    change(world.conn, "forge_item", review["item"]["id"], status="merged",
           labels=["awaiting-review", "review/source-review/running", "phase/postprocessing"])
    item = get(world.conn, "forge_item", review["item"]["id"])
    assert terminal_state(item) == "merged"
    first = terminal_label_operation(world.conn, world.service, review["actor"].id, item)
    second = terminal_label_operation(world.conn, world.service, review["actor"].id, item)
    assert first["id"] == second["id"]
    assert first["payload"]["add"] == ["review/merged"]
    assert set(first["payload"]["remove"]) == {"awaiting-review", "review/source-review/running"}


def test_closed_forge_items_use_explicit_superseded_marker(world, review):
    change(world.conn, "forge_item", review["item"]["id"], status="closed", labels=["superseded"])
    item = get(world.conn, "forge_item", review["item"]["id"])
    assert terminal_state(item) == "superseded"


def test_postprocessing_gate_requires_every_enabled_current_head_reviewer(world, review):
    change(world.conn, "forge_item", review["item"]["id"], review_phase="postprocessing")
    policy = get(world.conn, "review_policy", world.policy["id"])
    panel = postprocessing_review_panel(world.conn, get(world.conn, "forge_item", review["item"]["id"]), policy)
    assert panel["required"] == ["source-review"]
    assert panel["missing"] == ["source-review"]

    create(world.conn, "forge_review", forge_item_id=review["item"]["id"], remote_id="historical-panel",
           reviewer_remote_id="specialist", reviewer_descriptor_id=review["descriptor"]["id"], verdict="commented",
           summary="**Historical reviewer feedback:** old head", commit_oid=review["item"]["head_commit_oid"],
           observed_at=datetime.now(timezone.utc))
    assert postprocessing_review_panel(world.conn, get(world.conn, "forge_item", review["item"]["id"]), policy)["missing"] == ["source-review"]

    create(world.conn, "forge_review", forge_item_id=review["item"]["id"], remote_id="current-panel",
           reviewer_remote_id="specialist", reviewer_descriptor_id=review["descriptor"]["id"], verdict="changes_requested",
           summary="The current head needs a definition change.", commit_oid=review["item"]["head_commit_oid"],
           observed_at=datetime.now(timezone.utc))
    panel = postprocessing_review_panel(world.conn, get(world.conn, "forge_item", review["item"]["id"]), policy)
    assert "source-review" in panel["missing"]
    assert len(panel["unresolved_review_ids"]) == 1


def test_postprocessing_current_reports_cannot_be_one_line_approvals():
    with pytest.raises(DomainError) as error:
        validate_postprocessing_report("Looks good.", verdict="approved", historical=False)
    assert error.value.code == "review_report_incomplete"
    validate_postprocessing_report(
        """🔍 Scope
        The exact current PR head and all changed declarations were inspected.

        🔴 Blocking findings
        None found; each relevant risk was checked against the destination policy.

        ✅ Verified
        The public statements, definitions, consumers and imports were traced.

        🧪 Validation
        The focused build and the recorded package checks passed on the pinned head.

        💬 Verdict
        approved within this rubric, with no unresolved limitation in scope.
        """, verdict="approved", historical=False)


def test_concise_postprocessing_report_with_inline_evidence_and_limits():
    summary = """🔍 **Scope:** Metric.lean at abc123; public distance API.
🔴 **Findings:** None found.
✅ **Evidence:** distance_self reuses Mathlib; consumer check_metric.lean covers zero dimension.
🧪 **Limits:** Read the pinned passing check receipt; did not rerun the build.
💬 **Decision:** Approve this API scope; no performance assessment."""
    assert len(summary) < 600
    validate_postprocessing_report(summary, verdict="approved", historical=False)


@pytest.mark.parametrize("heading", ["Scope", "Findings", "Evidence", "Validation and limits", "Decision"])
def test_empty_report_sections_cannot_be_hidden_with_padding_elsewhere(heading):
    sections = {"Scope": "Metric.lean public declarations.", "Findings": "None found.",
                "Evidence": "check_metric.lean tests the zero-dimensional consumer.",
                "Validation and limits": "Passed receipt for abc123; no fresh build.",
                "Decision": "Approve the inspected scope."}
    sections[heading] = ""
    summary = "Unrelated padding " * 100 + "\n" + "\n".join(
        f"## {name}\n{body}" for name, body in sections.items())
    with pytest.raises(DomainError) as error:
        validate_postprocessing_report(summary, verdict="approved", historical=False)
    assert error.value.code == "review_report_incomplete"


def test_changes_requested_requires_a_finding_but_accepts_a_short_specific_blocker():
    summary = """Scope: Metric.lean at abc123.
Findings: None found.
Evidence: triangle_le uses an unproved symmetry hypothesis; see Metric.lean:42.
Validation: Static inspection only; no build.
Decision: Remove that hypothesis or justify the narrower API."""
    with pytest.raises(DomainError) as error:
        validate_postprocessing_report(summary, verdict="changes_requested", historical=False)
    assert error.value.code == "review_report_incomplete"
    validate_postprocessing_report(summary.replace("None found.", "Metric.lean:42 assumes the desired symmetry."),
                                   verdict="changes_requested", historical=False)


def test_historical_feedback_preserves_partial_findings():
    validate_postprocessing_report("Partial inspection: Metric.lean:42 needs a weaker hypothesis.",
                                   verdict="commented", historical=True)


def test_reviewer_report_cannot_impersonate_an_unprepared_or_other_execution(world, review):
    reviewer_account(world, review)
    data = ReviewerReport(verdict="commented", summary="Review evidence")
    with pytest.raises(DomainError) as error:
        report(world.conn, review["actor"], review["request"]["id"], data, world.service, "not-review")
    assert error.value.code == "scope_mismatch"
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    world.assignment(review["run"])
    other = authenticate(world.conn, world.claim()["execution_token"])
    with pytest.raises(DomainError) as error:
        report(world.conn, other, prepared["provider_request_id"], data, world.service, "other-execution")
    assert error.value.code == "scope_mismatch"
    with pytest.raises(DomainError):
        report(world.conn, world.actor, prepared["provider_request_id"], data, world.service, "not-own-token")


def test_reviewer_can_select_enabled_perspective_outside_phase_defaults(world, review):
    world.conn.execute(delete(tables["review_policy_reviewer"]).where(
        tables["review_policy_reviewer"].c.reviewer_descriptor_id == review["descriptor"]["id"]))
    result = prepare(world.conn, review["actor"], review["data"], world.service)
    assert result["status"] == "pending"


def test_review_dispatch_labels_are_queued_durably(world, review):
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    operation = world.conn.execute(select(tables["outbox_operation"]).where(
        tables["outbox_operation"].c.idempotency_key == "review-label:" + str(prepared["provider_request_id"]))).mappings().one()
    assert operation["payload"]["add"] == ["review/source-review/running"]
    assert "review/source-review/ok" in operation["payload"]["remove"]


def test_review_lifecycle_labels_use_policy_maintainer_identity(world, review):
    from archon_horizon.pipeline.reviews import queue_label
    repository = get(world.conn, "repository", review["item"]["repository_id"])
    identity = create(world.conn, "integration_identity", integration_id=repository["integration_id"],
        principal_id=world.actor.id, remote_user_id="maintainer", credential_ref="secret:maintainer")
    change(world.conn, "review_policy", world.policy["id"], maintainer_identity_id=identity["id"])
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    operation = world.conn.execute(select(tables["outbox_operation"]).where(
        tables["outbox_operation"].c.idempotency_key == "review-label:" + str(prepared["provider_request_id"]))).mappings().one()
    assert operation["payload"]["integration_identity_id"] == str(identity["id"])
    ordinary = queue_label(world.conn, review["actor"], {"forge_item_id": str(review["item"]["id"]),
        "add": ["awaiting-review"]}, "ordinary-attention-label")
    assert "integration_identity_id" not in ordinary["payload"]


@pytest.mark.parametrize("configured_maintainer", [False, True])
def test_review_labels_use_operational_identity_while_reports_keep_specialist_authorship(world, review, configured_maintainer):
    repository = get(world.conn, "repository", review["item"]["repository_id"])
    specialist = reviewer_account(world, review)
    maintainer = None
    if configured_maintainer:
        maintainer = create(world.conn, "integration_identity", integration_id=repository["integration_id"],
            principal_id=world.actor.id, remote_user_id="maintainer", credential_ref="secret:maintainer")
        change(world.conn, "review_policy", world.policy["id"], maintainer_identity_id=maintainer["id"])
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    operation = world.conn.execute(select(tables["outbox_operation"]).where(
        tables["outbox_operation"].c.idempotency_key == "review-label:" + str(prepared["provider_request_id"]))).mappings().one()
    assert operation["payload"].get("integration_identity_id") == (str(maintainer["id"]) if maintainer else None)
    attach(world.conn, review["actor"], prepared["provider_request_id"], ReviewerAttach(native_key="review-child"))
    assessment = report(world.conn, review["actor"], prepared["provider_request_id"],
        ReviewerReport(verdict="approved", summary="The statement and definitions agree with the intended result."),
        world.service, "specialist-assessment")
    assert assessment["payload"]["integration_identity_id"] == str(specialist["id"])
    assert assessment["payload"]["reviewer_descriptor_id"] == str(review["descriptor"]["id"])


def test_review_labels_do_not_fall_back_to_specialist_when_maintainer_identity_is_disabled(world, review):
    from archon_horizon.pipeline.review_labels import queue_state

    reviewer_account(world, review)
    repository = get(world.conn, "repository", review["item"]["repository_id"])
    maintainer = create(world.conn, "integration_identity", integration_id=repository["integration_id"],
        principal_id=world.actor.id, remote_user_id="maintainer", credential_ref="secret:maintainer", enabled=False)
    change(world.conn, "review_policy", world.policy["id"], maintainer_identity_id=maintainer["id"])
    descriptor = get(world.conn, "reviewer_descriptor", review["descriptor"]["id"])
    with pytest.raises(DomainError) as error:
        queue_state(world.conn, world.service, review["actor"].id, review["item"], descriptor, "running", "disabled-maintainer")
    assert error.value.code == "maintainer_identity_unavailable"


@pytest.mark.parametrize("delivery_status", ["pending", "running", "uncertain", "completed"])
def test_reservation_cleanup_preserves_review_verdict_label(world, review, delivery_status):
    reviewer_account(world, review)
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    attach(world.conn, review["actor"], prepared["provider_request_id"], ReviewerAttach(native_key="review-child"))
    operation = report(world.conn, review["actor"], prepared["provider_request_id"],
        ReviewerReport(verdict="changes_requested", summary="Remove the unnecessary hypothesis."),
        world.service, "own-assessment")
    change(world.conn, "outbox_operation", operation["id"], status=delivery_status)
    cancelled = cancel(world.conn, review["actor"], prepared["provider_request_id"],
        ReviewerCancel(note="Cleanup a stale lifecycle reservation"), service=world.service)
    assert cancelled["stop_required"] is True
    labels = world.conn.execute(select(tables["outbox_operation"].c.payload).where(
        tables["outbox_operation"].c.kind == "forge_label")).scalars()
    assert all("review/source-review/cancelled" not in payload["add"] for payload in labels)


@pytest.mark.parametrize("confirmed", [False, True])
def test_only_confirmed_execution_stop_settles_missing_child_events(world, review, confirmed):
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    attach(world.conn, review["actor"], prepared["provider_request_id"], ReviewerAttach(native_key="lost-terminal-event"))
    change(world.conn, "execution", review["execution"]["id"], status="succeeded" if confirmed else "lost",
        stop_confirmed_at=datetime.now(timezone.utc) if confirmed else None)
    world.scheduler.tick(world.conn)
    request = get(world.conn, "provider_request", prepared["provider_request_id"])
    assert request["status"] == ("interrupted" if confirmed else "submitted")
    if confirmed:
        assert request["failure"]["code"] == "execution_stop_confirmed"


@pytest.mark.parametrize("short_identity", [False, True])
def test_provider_rollout_terminal_observation_releases_native_reservation(world, review, short_identity):
    from test_pipeline_reviewer_invocations import observed, used
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    name = "hz_review_" + prepared["provider_request_id"].hex
    attach(world.conn, review["actor"], prepared["provider_request_id"],
        ReviewerAttach(native_key=str(uuid4())))
    cancel(world.conn, review["actor"], prepared["provider_request_id"], ReviewerCancel(note="Cleanup"))
    assert used(world, review) == 2
    change(world.conn, "provider_request", review["request"]["id"], status="completed",
        finished_at=datetime.now(timezone.utc))
    for identifier in world.conn.execute(select(tables["outbox_operation"].c.id)).scalars():
        change(world.conn, "outbox_operation", identifier, status="completed")
    world.command("checkpoint_assignment", get(world.conn, "assignment", review["assignment"]["id"]), note="Yield after reviews")
    assert not world.scheduler.heartbeat(world.conn, world.host_actor, review["execution"]["id"], review["grant"]["epoch"])["yield"]
    observed(world, review, {"type": "horizon.child_notifications", "children": [
        {"key": name if short_identity else "/root/" + name, "status": "completed"},
        {"key": "/root/unregistered", "status": "completed"}]})
    assert get(world.conn, "provider_request", prepared["provider_request_id"])["status"] == "completed"
    assert used(world, review) == 1
    assert world.scheduler.heartbeat(world.conn, world.host_actor, review["execution"]["id"], review["grant"]["epoch"])["yield"]
