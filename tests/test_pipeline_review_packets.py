import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from sqlalchemy import insert

from archon_horizon.pipeline.review.contracts import ReviewPlan
from archon_horizon.pipeline.persistence.records import change, create, get, object_ref
from archon_horizon.pipeline.review.packets import PACKET_BYTES, encode
from archon_horizon.pipeline.review.invocations import ReviewerReport, prepare, read
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_api import api, api_database, auth, mutate  # noqa: F401
from test_pipeline_reviewer_invocations import review  # noqa: F401
from test_pipeline_service import service_database, world  # noqa: F401


def context(world, manifest):
    artifact = get(world.conn, "artifact", manifest["review_context_artifact_id"])
    return artifact, json.loads(world.service.store.read(artifact["content"]["sha256"]))


def test_packet_supplies_pinned_rubric_valid_minimal_report_and_exact_evidence_links(world, review):
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    manifest = read(world.conn, world.actor, prepared["provider_request_id"], world.service)["manifest"]
    packet = manifest["review_packet"]
    artifact, full = context(world, manifest)
    assert packet["pins"]["head_commit_oid"] == review["item"]["head_commit_oid"]
    assert packet["pins"]["policy_revision_id"] == manifest["policy_revision_id"]
    assert packet["pins"]["reviewer_descriptor_revision_id"] == manifest["reviewer_descriptor_revision_id"]
    assert packet["descriptor"]["instructions"] == review["descriptor"]["instructions"]
    assert packet["review_instructions"] == "Focus on theorem 3"
    assert packet["complete_context"] == {"url": f"/api/v3/artifacts/{artifact['id']}/content",
                                           "sha256": artifact["content"]["sha256"]}
    for view in ("diff", "files", "comments", "reviews", "review_comments"):
        url = urlsplit(packet["evidence_links"][view])
        assert url.path == f"/api/v3/forge-items/{review['item']['id']}/inspect"
        params = parse_qs(url.query)
        assert params["view"] == [view]
        assert params["expected_head_oid"] == [review["item"]["head_commit_oid"]]
        assert params["limit"] == ["100" if view == "diff" else "20"]
        if view == "review_comments":
            assert params["review_id"] == ["{remote_review_number}"]
    assert "text (decoded UTF-8" in packet["response_fields"]["repository_file"]
    assert "report contains compiled" in packet["response_fields"]["milestone_check"]
    assert packet["report"]["url"] == f"/api/v3/reviewer-invocations/{prepared['provider_request_id']}/report"
    assert ReviewerReport.model_validate(packet["report"]["minimal_body"]).verdict == "commented"
    assert full["report_schema"] == ReviewerReport.model_json_schema()
    assert packet["report"]["delivery_url"] == "/api/v3/operations/{response.idempotency_key}?operation=reviewer_report"
    assert "do not repeat catalog, skill or schema discovery" in prepared["prompt"]
    assert "no omitted requirement is waived" in prepared["prompt"]
    assert "changed pins require reassessment" in encode(packet)
    assert len(encode(packet).encode()) <= PACKET_BYTES


def test_large_unicode_instructions_are_bounded_and_preserved_in_immutable_context(world, review):
    instructions = "\u5ba1\u67e5\U0001f50d" * 12000
    change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], instructions=instructions)
    change(world.conn, "review_policy", world.policy["id"], instructions=instructions + " policy")
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    manifest = read(world.conn, world.actor, prepared["provider_request_id"], world.service)["manifest"]
    packet = manifest["review_packet"]
    _, full = context(world, manifest)
    assert len(encode(packet).encode()) <= PACKET_BYTES
    for section in ("policy", "descriptor"):
        omitted = packet[section]
        assert omitted["omitted"] is True
        assert omitted["context_json_pointer"] == "/" + section
        assert omitted["encoded_bytes"] == len(encode(full[section]))
    assert full["descriptor"]["instructions"] == instructions
    assert full["policy"]["instructions"] == instructions + " policy"
    change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], instructions="Changed later")
    assert context(world, manifest)[1] == full
    assert "Changed later" not in prepared["prompt"]


def test_planned_review_packet_exposes_required_assessment_before_submission(world, review):
    plan = ReviewPlan(base_commit_oid="b" * 40, scope_paths=["Statements.lean"], questions=[
        {"dimension": "mathematical_contract", "question": "Do the repaired statements match the source?"},
        {"dimension": "public_design", "question": "Are the concrete definitions reusable?"}])
    prepared = prepare(world.conn, review["actor"], review["data"].model_copy(update={"review_plan": plan}), world.service)
    manifest = read(world.conn, world.actor, prepared["provider_request_id"], world.service)["manifest"]
    packet = manifest["review_packet"]
    contract = packet["report"]
    assert contract["assessment_required_for_approval"] is True
    template = contract["assessment_template"]
    assert template["dimensions"] == [question.dimension for question in plan.questions]
    assert template["complete"] is False and template["classification_confirmed"] is False
    report = ReviewerReport.model_validate({"verdict": "commented", "assessment": template})
    assert report.assessment is not None
    completed = {**template, "scope": "Inspected Statements.lean at the pinned head",
                 "complete": True, "classification_confirmed": True,
                 "evidence": ["Verified repaired statement and concrete definition against the cited source"]}
    assert ReviewerReport.model_validate({"verdict": "approved", "assessment": completed}).verdict == "approved"
    assert contract["typed_schema"]["context_json_pointer"] == "/report_schema"
    assert "summary alone cannot approve this plan" in prepared["prompt"]
    assert "A typed assessment is optional" not in prepared["prompt"]
    assert len(encode(packet).encode()) <= PACKET_BYTES


def test_packet_identifies_only_own_prior_objections_on_this_pr(world, review):
    other_descriptor = create(world.conn, "reviewer_descriptor", project_id=world.project["id"],
        slug="other-packet-reviewer", functions=["reviewer"], instructions="Review another dimension")
    other_item = create(world.conn, "forge_item", repository_id=review["item"]["repository_id"],
        remote_number=2, kind="pull_request", origin_run_id=review["run"]["id"], title="Other PR",
        status="open", head_commit_oid="c" * 40, observed_at=datetime.now(timezone.utc))
    prior = None
    for label, item_id, descriptor_id, verdict in (
        ("own-objection", review["item"]["id"], review["descriptor"]["id"], "changes_requested"),
        ("own-comment", review["item"]["id"], review["descriptor"]["id"], "commented"),
        ("other-reviewer", review["item"]["id"], other_descriptor["id"], "changes_requested"),
        ("other-pr", other_item["id"], review["descriptor"]["id"], "changes_requested"),
    ):
        row = create(world.conn, "forge_review", forge_item_id=item_id, remote_id=label,
            reviewer_remote_id="packet-reviewer", reviewer_descriptor_id=descriptor_id,
            verdict=verdict, summary="Missing the explicit endpoint bridge", commit_oid="b" * 40,
            observed_at=datetime.now(timezone.utc))
        if label == "own-objection":
            prior = row
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    manifest = read(world.conn, world.actor, prepared["provider_request_id"], world.service)["manifest"]
    packet = manifest["review_packet"]
    _, full = context(world, manifest)
    expected = {"id": str(prior["id"]), "commit_oid": "b" * 40,
                "summary": prior["summary"], "detail_url": f"/api/v3/records/forge_review/{prior['id']}"}
    assert full["prior_objections"] == [expected]
    assert packet["prior_objections"] == {
        "count": 1, "truncated": False, "context_json_pointer": "/prior_objections",
        "items": [{"id": expected["id"], "commit_oid": expected["commit_oid"],
                   "summary_excerpt": prior["summary"], "summary_truncated": False,
                   "detail_url": expected["detail_url"]}],
    }
    assert packet["report"]["assessment_required_for_approval"] is True
    assert "assessment.resolutions" in packet["report"]["approval_requirements"]
    assert "independent semantic decision" in packet["report"]["approval_requirements"]
    assert "assessment_template" not in packet["report"]
    assert len(encode(packet).encode()) <= PACKET_BYTES


def test_many_long_prior_objections_are_bounded_and_preserved_in_full_context(world, review):
    summary = "\u5ba1\u67e5\U0001f50d" * 1000
    rows = [create(world.conn, "forge_review", forge_item_id=review["item"]["id"],
        remote_id=f"prior-{index}", reviewer_remote_id="packet-reviewer",
        reviewer_descriptor_id=review["descriptor"]["id"], verdict="changes_requested",
        summary=summary + str(index), commit_oid="b" * 40, observed_at=datetime.now(timezone.utc))
        for index in range(9)]
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    manifest = read(world.conn, world.actor, prepared["provider_request_id"], world.service)["manifest"]
    packet = manifest["review_packet"]
    _, full = context(world, manifest)
    assert len(encode(packet).encode()) <= PACKET_BYTES
    prior = packet["prior_objections"]
    assert prior["count"] == 9 and prior["truncated"] is True
    assert prior["context_json_pointer"] == "/prior_objections"
    assert prior["items"]
    assert len(encode(prior)) <= 3200
    assert all(row["summary_truncated"] for row in prior["items"])
    assert [(row["id"], row["summary"]) for row in full["prior_objections"]] == [
        (str(row["id"]), row["summary"]) for row in rows]


def test_packet_missing_receipt_is_explicit_and_keeps_discovery_without_claimed_coverage(world, review):
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    manifest = read(world.conn, world.actor, prepared["provider_request_id"], world.service)["manifest"]
    packet = manifest["review_packet"]
    assert packet["milestone_evidence"] is None
    assert packet["milestone_discovery"]["check_id_field"] == "review_readiness.panel.check_id"
    assert "source/base commits" in prepared["prompt"]
    assert "missing or passed build evidence does not decide semantic quality" in prepared["prompt"]


def test_packet_receipt_url_tracks_actual_reviewer_report_delivery(api):
    client, database, world, run, _, _ = api
    with database.transaction() as conn:
        world.conn = conn
        world.disable_automations(run)
        world.assignment(run, role="maintainer")
        grant = world.claim()
        parent = create(conn, "provider_request", provider_thread_id=grant["provider_thread_record_id"],
            execution_id=grant["execution_id"], number=1, reason="assignment", status="running")
        repository = get(conn, "repository", world.document["source_repository_id"])
        identity = create(conn, "integration_identity", integration_id=repository["integration_id"],
            principal_id=world.actor.id, remote_user_id="packet-reviewer", credential_ref="secret:packet-test")
        descriptor = create(conn, "reviewer_descriptor", project_id=world.project["id"], slug="packet-reviewer",
            functions=["reviewer"], instructions="Inspect definitions", integration_identity_id=identity["id"])
        conn.execute(insert(tables["review_policy_reviewer"]).values(review_policy_id=world.policy["id"],
            reviewer_descriptor_id=descriptor["id"]))
        item = create(conn, "forge_item", repository_id=repository["id"], remote_number=1,
            kind="pull_request", origin_run_id=run["id"], review_phase="preprocessing", target_branch="main",
            title="Review", status="open", head_commit_oid="a" * 40, observed_at=datetime.now(timezone.utc))
        checked = create(conn, "milestone_check", project_id=world.project["id"],
            repository_id=repository["id"], principal_id=world.actor.id, source_commit_oid="a" * 40,
            base_commit_oid="b" * 40, kind="contract", report={"compiled": True, "types": {"Demo": []},
                "definitions": {}, "targets": {"Demo": ["sorryAx"]}, "direct_admissions": ["Demo"]},
            report_sha256="e" * 64)
    token = grant["execution_token"]
    response = mutate(client, "/api/v3/reviewer-invocations", {
        "parent_request_id": str(parent["id"]), "forge_item_id": str(item["id"]),
        "reviewer_descriptor_id": str(descriptor["id"]), "expected_head_oid": "a" * 40}, token)
    assert response.status_code == 200, response.text
    request_id = response.json()["provider_request_id"]
    detail = client.get(f"/api/v3/reviewer-invocations/{request_id}", headers=auth(token)).json()
    packet = detail["manifest"]["review_packet"]
    evidence = client.get(packet["milestone_evidence"]["check_url"], headers=auth(token))
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()["id"] == str(checked["id"])
    assert evidence.json()["report"]["types"] == {"Demo": []}
    assert evidence.json()["report"]["compiled"] is True
    attached = mutate(client, f"/api/v3/reviewer-invocations/{request_id}/attach",
        {"native_key": "actual-packet-child"}, token)
    assert attached.status_code == 200, attached.text
    key = str(uuid4())
    submitted = mutate(client, packet["report"]["url"], packet["report"]["minimal_body"], token, key)
    assert submitted.status_code == 200, submitted.text
    receipt = submitted.json()
    assert receipt["idempotency_key"] == key and receipt["id"] != key
    assert receipt["kind"] == "forge_review"
    url = packet["report"]["delivery_url"].replace("{response.idempotency_key}", receipt["idempotency_key"])
    assert client.get(url, headers=auth(token)).json()["status"] == "pending"
    assert client.get(url.replace("reviewer_report", "forge_review"), headers=auth(token)).status_code == 404
    assert client.get(url.replace(key, receipt["id"]), headers=auth(token)).status_code == 404
    with database.transaction() as conn:
        delivered = create(conn, "forge_review", forge_item_id=item["id"], remote_id="packet-report",
            reviewer_remote_id="packet-reviewer", provider_request_id=request_id, verdict="commented",
            summary="Definitions inspected", commit_oid="a" * 40, observed_at=datetime.now(timezone.utc))
        change(conn, "outbox_operation", receipt["id"], status="completed",
            result_ref_id=object_ref(conn, "forge_review", delivered["id"]))
    completed = client.get(url, headers=auth(token))
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "completed" and completed.json()["result_ref_id"]
    assert client.get(f"/api/v3/reviewer-invocations/{request_id}", headers=auth(token)).json()["request"]["status"] == "submitted"
