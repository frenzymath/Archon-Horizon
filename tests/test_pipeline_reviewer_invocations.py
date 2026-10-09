from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, insert, select

from archon_horizon.pipeline.auth import authenticate
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.providers.provider_events import child_state_ref, project_observation
from archon_horizon.pipeline.persistence.records import change, create, get
from archon_horizon.pipeline.review.invocations import ReviewerAttach, ReviewerCancel, ReviewerPrepare, attach, cancel, prepare, read
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_service import service_database, world  # noqa: F401


@pytest.fixture
def review(world):
    limit = create(world.conn, "resource_limit", kind="provider_account", slug="review-provider", max_concurrent=3)
    world.conn.execute(insert(tables["host_harness_limit"]).values(host_id=world.host["id"], harness_id=world.harness["id"], resource_limit_id=limit["id"]))
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run, role="maintainer")
    grant = world.claim()
    actor = authenticate(world.conn, grant["execution_token"])
    execution = get(world.conn, "execution", grant["execution_id"])
    thread = get(world.conn, "provider_thread", grant["provider_thread_record_id"])
    request = create(world.conn, "provider_request", provider_thread_id=thread["id"], execution_id=execution["id"],
                     number=1, reason="assignment", status="running")
    descriptor = create(world.conn, "reviewer_descriptor", project_id=world.project["id"], slug="source-review",
                        functions=["reviewer"], instructions="Inspect mathematical source alignment", model_options={"model": "review-model"})
    world.conn.execute(insert(tables["review_policy_reviewer"]).values(review_policy_id=world.policy["id"], reviewer_descriptor_id=descriptor["id"]))
    world.conn.execute(insert(tables["reviewer_guidance"]).values(reviewer_descriptor_id=descriptor["id"], document_id=world.document["id"]))
    item = create(world.conn, "forge_item", repository_id=world.document["source_repository_id"], remote_number=1,
        kind="pull_request", origin_run_id=run["id"], review_phase="preprocessing", target_branch="main", title="New statements",
        status="open", head_commit_oid="a" * 40, observed_at=datetime.now(timezone.utc))
    data = ReviewerPrepare(parent_request_id=request["id"], forge_item_id=item["id"], reviewer_descriptor_id=descriptor["id"],
                           expected_head_oid=item["head_commit_oid"], instructions="Focus on theorem 3")
    return dict(run=run, assignment=assignment, actor=actor, grant=grant, execution=execution, thread=thread,
                request=request, descriptor=descriptor, item=item, data=data, limit=limit)


def observed(world, review, raw):
    return project_observation(world.conn, world.host_actor, review["execution"], review["thread"], review["request"]["id"],
        "codex", raw, uuid4(), datetime.now(timezone.utc), world.project["id"])


def used(world, review):
    claim = tables["resource_claim"]
    return world.conn.execute(select(func.coalesce(func.sum(claim.c.units), 0)).where(
        claim.c.resource_limit_id == review["limit"]["id"], claim.c.released_at.is_(None))).scalar_one()


def test_prepare_pins_descriptor_guidance_policy_head_and_permissions(world, review):
    result = prepare(world.conn, review["actor"], review["data"], world.service)
    assert result["status"] == "pending"
    assert result["model_options"]["model"] == "review-model"
    assert "Focus on theorem 3" in result["prompt"]
    assert result["task_name"] == "hz_review_" + result["provider_request_id"].hex
    assert f"Horizon project UUID: {world.project['id']}" in result["prompt"]
    assert f"repository UUID: {review['item']['repository_id']}" in result["prompt"]
    assert f"Forge item UUID: {review['item']['id']}" in result["prompt"]
    assert f"GET /api/v3/forge-items/{review['item']['id']}/inspect?view=diff&expected_head_oid={'a' * 40}" in result["prompt"]
    assert "/api/v3/forge-items/1/inspect" not in result["prompt"]
    assert "target branch: main" in result["prompt"]
    assert "agent schema --section operations --name reviewer_report" in result["prompt"]
    assert f"agent request POST /api/v3/reviewer-invocations/{result['provider_request_id']}/report JSON" in result["prompt"]
    assert '"verdict":"commented","summary":"Your assessment and evidence"' in result["prompt"]
    assert 'mktemp -d "$TMPDIR/horizon-review.XXXXXX"' in result["prompt"]
    assert "Do not hard-code /tmp" in result["prompt"]
    assert "Host-managed lean-build directories" in result["prompt"]
    assert "do not enter them to run builds" in result["prompt"]
    assert "Do not duplicate a full project or dependency build" in result["prompt"]
    assert "Do not wait for your own approval/readiness gate" in result["prompt"]
    assert "Then finish your native review turn" in result["prompt"]
    pinned = read(world.conn, world.actor, result["provider_request_id"], world.service)
    assert pinned["manifest"]["head_commit_oid"] == "a" * 40
    assert pinned["manifest"]["sandbox_manifest_artifact_id"] == str(review["execution"]["sandbox_manifest_artifact_id"])
    change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], instructions="Changed rubric")
    change(world.conn, "document", world.document["id"], source_commit_oid="b" * 40)
    later = read(world.conn, world.actor, result["provider_request_id"], world.service)
    assert later["manifest"] == pinned["manifest"]
    assert "Changed rubric" not in later["manifest"]["prompt"]
    assert later["manifest"]["guidance"][0]["source_commit_oid"] == "a" * 40
    assert used(world, review) == 2
    assert any(item["kind"] == "review" and str(result["provider_request_id"]) in item["description"]
               for item in world.ledger(review["assignment"]["id"]))


@pytest.mark.parametrize("kind,job_state", [("route", "completed"), ("contract", "completed"), ("route", "failed")])
def test_prepare_exposes_pinned_check_and_distinct_matching_verification_job(world, review, kind, job_state):
    checked = create(world.conn, "milestone_check", project_id=world.project["id"],
        repository_id=review["item"]["repository_id"], principal_id=world.actor.id,
        source_commit_oid=review["item"]["head_commit_oid"], base_commit_oid="b" * 40, kind=kind,
        report={"compiled": True, "manifest_digest": "c" * 64, "base_manifest_digest": "d" * 64,
                "toolchain": "leanprover/lean4:v4.19.0"}, report_sha256="e" * 64)
    job = create(world.conn, "milestone_job", project_id=world.project["id"], host_id=world.host["id"],
        workspace_id=review["execution"]["workspace_id"], principal_id=world.actor.id,
        request={"source_commit_oid": checked["source_commit_oid"], "base_commit_oid": checked["base_commit_oid"]},
        status=job_state, check_id=checked["id"])
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    manifest = read(world.conn, world.actor, prepared["provider_request_id"], world.service)["manifest"]
    evidence = manifest["milestone_evidence"]
    assert evidence["kind"] == kind and evidence["status"] == "passed"
    assert evidence["source_commit_oid"] == review["item"]["head_commit_oid"]
    assert evidence["base_commit_oid"] == "b" * 40 and evidence["compiled"] is True
    assert evidence["check_url"] == f"/api/v3/lean/checks/{checked['id']}"
    if job_state == "completed":
        assert evidence["job"] == {"id": str(job["id"]), "status": "completed",
                                   "url": f"/api/v3/lean/verifications/{job['id']}"}
    else:
        assert "job" not in evidence
    assert f"/api/v3/lean/verifications/{checked['id']}" not in prepared["prompt"]
    assert "only for a concrete concern" in prepared["prompt"]
    assert "does not establish semantic correctness" in prepared["prompt"]


def test_prepare_omits_stale_check_instead_of_inventing_current_coverage(world, review):
    stale = create(world.conn, "milestone_check", project_id=world.project["id"],
        repository_id=review["item"]["repository_id"], principal_id=world.actor.id,
        source_commit_oid="f" * 40, base_commit_oid="b" * 40, kind="route",
        report={"compiled": True}, report_sha256="e" * 64)
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    manifest = read(world.conn, world.actor, prepared["provider_request_id"], world.service)["manifest"]
    assert "milestone_evidence" not in manifest
    assert str(stale["id"]) not in prepared["prompt"]
    assert manifest["milestone_discovery"] == {
        "url": f"/api/v3/forge-items/{review['item']['id']}/inspect?view=reviews&expected_head_oid={'a' * 40}&limit=1",
        "check_id_field": "review_readiness.panel.check_id"}
    assert "/api/v3/lean/checks/{check_id}" in prepared["prompt"]


@pytest.mark.parametrize("status", ["queued", "running"])
def test_prepare_discovers_matching_pending_verification_before_receipt(world, review, status):
    workspace = create(world.conn, "workspace", project_id=world.project["id"], host_id=world.host["id"],
        repository_id=review["item"]["repository_id"], path="/review-roadmap", branch_name="review",
        base_commit_oid="b" * 40, status="ready")
    matching = create(world.conn, "milestone_job", project_id=world.project["id"], host_id=world.host["id"],
        workspace_id=workspace["id"], principal_id=world.actor.id,
        request={"source_commit_oid": "a" * 40, "base_commit_oid": "b" * 40,
                 "solution_workspace_id": None}, status=status)
    rejected = []
    for workspace_id, head, solution in ((review["execution"]["workspace_id"], "a" * 40, None),
                                       (workspace["id"], "f" * 40, None),
                                       (workspace["id"], "a" * 40, str(workspace["id"]))):
        rejected.append(create(world.conn, "milestone_job", project_id=world.project["id"], host_id=world.host["id"],
            workspace_id=workspace_id, principal_id=world.actor.id, status="running",
            request={"source_commit_oid": head, "base_commit_oid": "c" * 40,
                     "solution_workspace_id": solution}))
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    manifest = read(world.conn, world.actor, prepared["provider_request_id"], world.service)["manifest"]
    assert "milestone_evidence" not in manifest
    assert manifest["milestone_discovery"]["pending_job"] == {
        "id": str(matching["id"]), "status": status,
        "url": f"/api/v3/lean/verifications/{matching['id']}",
        "source_commit_oid": "a" * 40, "base_commit_oid": "b" * 40}
    assert all(str(job["id"]) not in prepared["prompt"] for job in rejected)
    assert "Continue semantic and source assessment while verification runs" in prepared["prompt"]


@pytest.mark.parametrize("label_source", ["task_name", "prompt"])
def test_native_label_reconciles_prepared_reviewer_before_attach_and_releases_capacity(world, review, label_source):
    result = prepare(world.conn, review["actor"], review["data"], world.service)
    spawn = {"type": "item.completed", "item": {"id": "native-call", "type": "collab_tool_call", "tool": "spawn_agent",
        label_source: result["task_name"] if label_source == "task_name" else result["prompt"],
        "receiver_thread_ids": ["native-review-thread"]}}
    observed(world, review, spawn)
    observed(world, review, spawn)
    request = get(world.conn, "provider_request", result["provider_request_id"])
    assert request["status"] == "running"
    assert request["reason"] == "review" and request["reviewer_descriptor_id"] == review["descriptor"]["id"]
    threads = tables["provider_thread"]
    assert world.conn.execute(select(func.count()).select_from(threads).where(threads.c.kind == "child")).scalar_one() == 1
    attached = attach(world.conn, review["actor"], request["id"], ReviewerAttach(native_key="native-review-thread", native_invocation_id="native-call"))
    assert attached["provider_thread_id"] == result["provider_thread_id"]
    observed(world, review, {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "wait",
        "agents_states": {"native-review-thread": {"status": "completed"}}}})
    assert get(world.conn, "provider_request", request["id"])["status"] == "completed"
    assert used(world, review) == 1


@pytest.mark.parametrize("binding", ["attach", "prompt", "rollout"])
def test_observed_native_child_binds_pending_review_without_losing_history_or_double_capacity(world, review, binding):
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    native_id = str(uuid4())
    spawn = {"type": "item.completed", "item": {"id": "native-call", "type": "collab_tool_call", "tool": "spawn_agent",
        "receiver_thread_ids": [native_id]}}
    observed(world, review, spawn)
    threads, requests, usage, activities = (tables[name] for name in ("provider_thread", "provider_request", "usage_record", "activity"))
    actual = dict(world.conn.execute(select(threads).where(threads.c.provider_thread_id == native_id)).mappings().one())
    original = dict(world.conn.execute(select(requests).where(requests.c.provider_thread_id == actual["id"])).mappings().one())
    counters = {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "wait",
        "agents_states": {native_id: {"status": "running", "usage": {"input_tokens": 12, "output_tokens": 3}}}}}
    observed(world, review, counters)
    evidence = dict(world.conn.execute(select(usage).where(usage.c.provider_thread_id == actual["id"])).mappings().one())
    activity_ids = set(world.conn.execute(select(activities.c.id)).scalars())
    assert used(world, review) == 3
    if binding == "attach":
        result = attach(world.conn, review["actor"], prepared["provider_request_id"],
            ReviewerAttach(native_key=native_id, native_invocation_id="native-call"))
        assert result["provider_thread_id"] == prepared["provider_thread_id"]
    elif binding == "prompt":
        observed(world, review, {**spawn, "item": {**spawn["item"], "prompt": prepared["prompt"]}})
    else:
        observed(world, review, {"type": "horizon.child_notifications", "children": [{
            "key": "/root/" + prepared["task_name"], "native_id": native_id, "invocation_id": "native-call",
            "status": "running", "discovered": True}]})
    bound = get(world.conn, "provider_request", prepared["provider_request_id"])
    assert bound["provider_thread_id"] == prepared["provider_thread_id"] and bound["status"] == "running"
    assert bound["reason"] == "review" and bound["reviewer_descriptor_id"] == review["descriptor"]["id"]
    assert bound["provider_turn_id"] == "native-call"
    assert bound["started_at"] == original["started_at"]
    assert bound["guidance_manifest_artifact_id"] == prepared["manifest_artifact_id"]
    duplicate = get(world.conn, "provider_request", original["id"])
    assert duplicate["status"] == "interrupted" and duplicate["failure"]["code"] == "review_binding_reconciled"
    assert get(world.conn, "provider_thread", actual["id"])["status"] == "closed"
    assert get(world.conn, "provider_thread", prepared["provider_thread_id"])["provider_thread_id"] == native_id
    assert get(world.conn, "usage_record", evidence["id"]) == evidence
    assert activity_ids <= set(world.conn.execute(select(activities.c.id)).scalars())
    assert world.conn.execute(select(func.count()).select_from(tables["forge_review"])).scalar_one() == 0
    assert used(world, review) == 2
    observed(world, review, spawn)
    observed(world, review, counters)
    assert world.conn.execute(select(func.sum(usage.c.input_tokens))).scalar_one() == 12
    assert world.conn.execute(select(func.sum(usage.c.output_tokens))).scalar_one() == 3
    counters["item"]["agents_states"][native_id]["usage"] = {"input_tokens": 20, "output_tokens": 5}
    observed(world, review, counters)
    assert world.conn.execute(select(func.sum(usage.c.input_tokens))).scalar_one() == 20
    assert world.conn.execute(select(func.sum(usage.c.output_tokens))).scalar_one() == 5
    attach(world.conn, review["actor"], prepared["provider_request_id"], ReviewerAttach(native_key=native_id))
    assert get(world.conn, "provider_request", original["id"])["status"] == "interrupted"
    assert get(world.conn, "provider_request", prepared["provider_request_id"])["status"] == "running"
    assert used(world, review) == 2
    observed(world, review, {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "wait",
        "agents_states": {native_id: {"status": "completed"}}}})
    assert get(world.conn, "provider_request", prepared["provider_request_id"])["status"] == "completed"
    assert used(world, review) == 1


@pytest.mark.parametrize("conflict", ["terminal", "parent", "execution", "review", "invocation"])
def test_native_binding_preserves_ambiguous_or_terminal_observed_children(world, review, conflict):
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    native_id = str(uuid4())
    parent_id = review["request"]["id"]
    execution_id = review["execution"]["id"]
    if conflict == "parent":
        other_parent = create(world.conn, "provider_request", provider_thread_id=review["thread"]["id"],
            execution_id=review["execution"]["id"], number=2, reason="continuation", status="completed")
        parent_id = other_parent["id"]
    elif conflict == "execution":
        world.assignment(review["run"])
        execution_id = world.claim()["execution_id"]
    thread = create(world.conn, "provider_thread", assignment_id=review["assignment"]["id"], number=3, kind="child",
        parent_request_id=parent_id, workspace_id=review["thread"]["workspace_id"],
        harness_revision_id=review["thread"]["harness_revision_id"], skill_bundle_artifact_id=review["thread"]["skill_bundle_artifact_id"],
        provider_thread_id=native_id, provider_state_ref=child_state_ref(review["thread"]["id"], "codex", native_id),
        applied_model_options={}, status="available")
    original = create(world.conn, "provider_request", provider_thread_id=thread["id"], execution_id=execution_id,
        number=1, reason="review" if conflict == "review" else "continuation", provider_turn_id="native-call",
        reviewer_descriptor_id=review["descriptor"]["id"] if conflict == "review" else None,
        status="completed" if conflict == "terminal" else "running")
    before = get(world.conn, "provider_request", original["id"])
    with pytest.raises(DomainError) as error:
        attach(world.conn, review["actor"], prepared["provider_request_id"], ReviewerAttach(native_key=native_id,
            native_invocation_id="different-call" if conflict == "invocation" else "native-call"))
    assert error.value.code == "review_identity_conflict"
    assert get(world.conn, "provider_request", original["id"]) == before
    assert get(world.conn, "provider_request", prepared["provider_request_id"])["status"] == "pending"


@pytest.mark.parametrize("prefix", ["", "/root/"])
def test_native_attach_rejects_task_label_substitution(world, review, prefix):
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    with pytest.raises(DomainError) as error:
        attach(world.conn, review["actor"], prepared["provider_request_id"], ReviewerAttach(native_key=prefix + prepared["task_name"]))
    assert error.value.code == "invalid_arguments" and error.value.status == 422
    assert "actual child ID" in str(error.value) and "does not launch" in str(error.value)
    assert get(world.conn, "provider_request", prepared["provider_request_id"])["status"] == "pending"
    assert get(world.conn, "provider_thread", prepared["provider_thread_id"])["provider_thread_id"] is None


@pytest.mark.parametrize("attached", [False, True])
def test_terminal_task_label_never_overrides_missing_or_mismatched_native_identity(world, review, attached):
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    if attached:
        attach(world.conn, review["actor"], prepared["provider_request_id"], ReviewerAttach(native_key=str(uuid4())))
    before = get(world.conn, "provider_request", prepared["provider_request_id"])
    observed(world, review, {"type": "horizon.child_notifications", "children": [{
        "key": "/root/" + prepared["task_name"], "native_id": str(uuid4()), "status": "completed"}]})
    assert get(world.conn, "provider_request", prepared["provider_request_id"]) == before
    assert used(world, review) == 2


def test_terminal_review_alias_survives_primary_request_continuation(world, review):
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    attach(world.conn, review["actor"], prepared["provider_request_id"], ReviewerAttach(native_key=str(uuid4())))
    change(world.conn, "provider_request", review["request"]["id"], status="completed", finished_at=func.now())
    continuation = create(world.conn, "provider_request", provider_thread_id=review["thread"]["id"],
        execution_id=review["execution"]["id"], number=2, reason="continuation", status="running")
    observed(world, {**review, "request": continuation}, {"type": "horizon.child_notifications", "children": [{
        "key": "/root/" + prepared["task_name"], "status": "completed", "discovered": True}]})
    assert get(world.conn, "provider_request", prepared["provider_request_id"])["status"] == "completed"
    assert used(world, review) == 1


def test_shared_provider_budget_bounds_reviews_and_parent_worker_claims(world, review):
    first = prepare(world.conn, review["actor"], review["data"], world.service)
    second_item = create(world.conn, "forge_item", repository_id=review["item"]["repository_id"],
        remote_number=2, kind="pull_request", origin_run_id=review["run"]["id"],
        review_phase="preprocessing", target_branch="main", title="Another contract",
        status="open", head_commit_oid="b" * 40, observed_at=func.now())
    second_data = review["data"].model_copy(update={"forge_item_id": second_item["id"], "expected_head_oid": "b" * 40})
    prepare(world.conn, review["actor"], second_data, world.service)
    assert used(world, review) == 3
    change(world.conn, "forge_item", second_item["id"], head_commit_oid="c" * 40)
    with pytest.raises(DomainError) as error:
        prepare(world.conn, review["actor"], second_data.model_copy(update={"expected_head_oid": "c" * 40}), world.service)
    assert error.value.code == "review_capacity_unavailable"
    world.assignment(review["run"])
    assert world.claim() is None
    cancelled = cancel(world.conn, review["actor"], first["provider_request_id"], ReviewerCancel(note="Review this part directly"))
    assert cancelled["status"] == "interrupted" and not cancelled["stop_required"]
    assert used(world, review) == 2
    assert world.claim() is not None


def test_repeated_review_preparation_reuses_owner_before_checking_capacity(world, review):
    first = prepare(world.conn, review["actor"], review["data"], world.service)
    change(world.conn, "resource_limit", review["limit"]["id"], max_concurrent=2)
    second = prepare(world.conn, review["actor"], review["data"], world.service)
    assert second["provider_request_id"] == first["provider_request_id"]
    assert used(world, review) == 2
    assert world.conn.execute(select(func.count()).select_from(tables["review_work"])).scalar_one() == 1


def test_review_retry_after_repair_has_no_lifetime_cap_and_preserves_owner_fences(world, review):
    first = prepare(world.conn, review["actor"], review["data"], world.service)
    retry = review["data"].model_copy(update={"retry_of": first["provider_request_id"], "retry_note": "Provider interrupted"})
    with pytest.raises(DomainError) as error:
        prepare(world.conn, review["actor"], retry, world.service)
    assert error.value.code == "review_unsettled"
    current = first
    for number in (2, 3, 4, 5):
        cancel(world.conn, review["actor"], current["provider_request_id"], ReviewerCancel(note="Provider interrupted before launch"))
        duplicate = prepare(world.conn, review["actor"], review["data"], world.service)
        assert duplicate["provider_request_id"] == current["provider_request_id"]
        assert used(world, review) == 1
        previous = current
        current = prepare(world.conn, review["actor"], review["data"].model_copy(update={
            "retry_of": previous["provider_request_id"],
            "retry_note": "Repaired native child correlation and verified the previous launch stopped"}), world.service)
        assert world.conn.execute(select(tables["review_work"].c.attempts)).scalar_one() == number
        manifest = read(world.conn, world.actor, current["provider_request_id"], world.service)["manifest"]
        assert manifest["retry_of"] == str(previous["provider_request_id"])
        assert manifest["retry_note"] == "Repaired native child correlation and verified the previous launch stopped"
        assert manifest["head_commit_oid"] == review["item"]["head_commit_oid"]
        assert used(world, review) == 2
        with pytest.raises(DomainError) as error:
            prepare(world.conn, review["actor"], review["data"].model_copy(update={
                "retry_of": previous["provider_request_id"], "retry_note": "Replayed recovery request"}), world.service)
        assert error.value.code == "revision_conflict"
    assert world.conn.execute(select(func.count()).select_from(tables["review_work"])).scalar_one() == 1
    assert get(world.conn, "provider_request", first["provider_request_id"])["status"] == "interrupted"
    change(world.conn, "provider_request", current["provider_request_id"], status="completed")
    with pytest.raises(DomainError) as error:
        prepare(world.conn, review["actor"], review["data"].model_copy(update={
            "retry_of": current["provider_request_id"], "retry_note": "A completed result needs a different vote"}), world.service)
    assert error.value.code == "review_unsettled"


@pytest.mark.parametrize("retry_fields", [
    {"retry_of": str(uuid4())}, {"retry_note": "Repaired launch transport"},
    {"retry_of": str(uuid4()), "retry_note": "   "},
])
def test_review_retry_requires_owner_and_nonblank_repair_note(world, review, retry_fields):
    with pytest.raises(ValidationError):
        ReviewerPrepare.model_validate({**review["data"].model_dump(), **retry_fields})


def test_attached_cancellation_keeps_capacity_until_native_stop_is_observed(world, review):
    result = prepare(world.conn, review["actor"], review["data"], world.service)
    attach(world.conn, review["actor"], result["provider_request_id"], ReviewerAttach(native_key="child"))
    cancelled = cancel(world.conn, review["actor"], result["provider_request_id"], ReviewerCancel(note="The PR was superseded"))
    assert cancelled["status"] == "uncertain" and cancelled["stop_required"]
    assert used(world, review) == 2
    observed(world, review, {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "wait",
        "agents_states": {"child": {"status": "cancelled"}}}})
    assert used(world, review) == 1
    assert get(world.conn, "provider_request", result["provider_request_id"])["status"] == "interrupted"


def test_prepare_rejects_stale_head_wrong_policy_and_expired_or_nonagent_callers(world, review):
    with pytest.raises(DomainError) as error:
        prepare(world.conn, world.actor, review["data"], world.service)
    assert error.value.code == "forbidden"
    change(world.conn, "forge_item", review["item"]["id"], head_commit_oid="b" * 40)
    with pytest.raises(DomainError) as error:
        prepare(world.conn, review["actor"], review["data"], world.service)
    assert error.value.code == "review_head_changed"
    change(world.conn, "forge_item", review["item"]["id"], head_commit_oid="a" * 40, review_phase="postprocessing")
    with pytest.raises(DomainError) as error:
        prepare(world.conn, review["actor"], review["data"], world.service)
    assert error.value.code == "review_policy_unavailable"
    change(world.conn, "forge_item", review["item"]["id"], review_phase="preprocessing")
    world.scheduler.cancel(world.conn, world.actor, get(world.conn, "assignment", review["assignment"]["id"]), "Stop")
    with pytest.raises(DomainError) as error:
        prepare(world.conn, review["actor"], review["data"], world.service)
    assert error.value.code == "stale_epoch"


def test_unregistered_native_children_still_reduce_shared_account_capacity(world, review):
    observed(world, review, {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "spawn_agent",
        "task_name": "hz_review_" + uuid4().hex, "receiver_thread_ids": ["unregistered"]}})
    assert used(world, review) == 2
    request = world.conn.execute(select(tables["provider_request"]).where(
        tables["provider_request"].c.id != review["request"]["id"])).mappings().one()
    assert request["reviewer_descriptor_id"] is None
    observed(world, review, {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "wait",
        "agents_states": {"unregistered": {"status": "completed"}}}})
    assert used(world, review) == 1


def test_half_open_provider_probe_cannot_fan_out_review_children(world, review):
    change(world.conn, "resource_limit", review["limit"]["id"], failure_count=1)
    with pytest.raises(DomainError) as error:
        prepare(world.conn, review["actor"], review["data"], world.service)
    assert error.value.code == "review_capacity_unavailable"
    assert used(world, review) == 1
