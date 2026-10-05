"""Pinned durable reviewers and native invitations with provider reservations.

Preparation authorizes one invocation but never launches it. The caller uses
the returned native task label and prompt, then attaches the provider identity.
Retries reuse the exact-head contract; a failed attempt needs an explicit
recovery decision before another provider can run.
"""

from __future__ import annotations

import json
from typing import Literal
from urllib.parse import urlencode
from uuid import UUID, uuid4

from pydantic import Field, StrictBool, model_validator
from sqlalchemy import func, insert, select, update

from .auth import live_execution, require_project
from .errors import DomainError
from .models import AssignmentCreate, Contract, ForgeReviewComment, MissionCreate, ModelOptions, Text
from .records import change, create, emit, get, json_value, next_number, project_of, same_project, save_blob, snapshot
from .review_contracts import ReviewAssessment, ReviewPlan
from .schema import tables

ACTIVE = ("pending", "submitted", "running", "uncertain")
TERMINAL = ("completed", "failed", "interrupted")


class ReviewerPrepare(Contract):
    parent_request_id: UUID
    forge_item_id: UUID
    reviewer_descriptor_id: UUID
    expected_head_oid: Text | None = None
    instructions: str = Field(default="", max_length=16000)
    review_plan: ReviewPlan | None = None
    retry_of: UUID | None = None
    retry_note: Text | None = Field(default=None,
        description="Explain the diagnosed failure and the concrete repair that makes this retry useful.")

    @model_validator(mode="after")
    def explicit_retry(self):
        if (self.retry_of is None) != (self.retry_note is None):
            raise ValueError("review recovery requires both retry_of and retry_note")
        return self


class ReviewerAttach(Contract):
    native_key: Text = Field(max_length=256)
    native_invocation_id: Text | None = Field(default=None, max_length=256)
    background: StrictBool | None = None


class ReviewerCancel(Contract):
    note: Text


class ReviewerReport(Contract):
    verdict: Literal["approved", "changes_requested", "commented"]
    summary: Text | None = None
    assessment: ReviewAssessment | None = None
    comments: list[ForgeReviewComment] = Field(default_factory=list, max_length=100)
    historical: StrictBool = False


def report(conn, actor, request_id, data: ReviewerReport, service, key):
    """A reviewer publishes its own pinned assessment before returning to its parent."""
    from .reviews import queue_review
    from .scheduler import Scheduler
    execution = live_execution(conn, actor, lock=True)
    request = get(conn, "provider_request", request_id, lock=True)
    if request["execution_id"] != execution["id"] or request["reason"] != "review":
        raise DomainError("scope_mismatch", "Reviewer report belongs to another invocation", 403)
    if not request["guidance_manifest_artifact_id"] or not request["reviewer_descriptor_id"]:
        raise DomainError("review_manifest_missing", "Reviewer requires pinned provenance")
    artifact = get(conn, "artifact", request["guidance_manifest_artifact_id"])
    manifest = json.loads(service.store.read(artifact["content"]["sha256"]))
    return queue_review(conn, actor, service, Scheduler(service), {
        **data.model_dump(mode="json"), "forge_item_id": manifest["forge_item_id"],
        "commit_oid": manifest["head_commit_oid"], "provider_request_id": str(request_id),
        "reviewer_descriptor_id": str(request["reviewer_descriptor_id"])}, key, reviewer_submission=True)


def _maintainer(conn, actor):
    execution = live_execution(conn, actor, lock=True)
    if execution["role"] != "maintainer":
        raise DomainError("forbidden", "Only the live maintainer can prepare or manage native reviewers", 403)
    return execution


def _owned(conn, actor, request_id):
    execution = _maintainer(conn, actor)
    request = get(conn, "provider_request", request_id, lock=True)
    thread = get(conn, "provider_thread", request["provider_thread_id"], lock=True)
    if request["execution_id"] != execution["id"] or thread["assignment_id"] != execution["assignment_id"]:
        raise DomainError("scope_mismatch", "Reviewer belongs to another execution", 403)
    if request["reason"] != "review" or thread["kind"] != "child":
        raise DomainError("not_review_invocation", "Request is not a prepared native reviewer", 422)
    return execution, request, thread


def provider_limits(conn, execution):
    limits, links = tables["resource_limit"], tables["host_harness_limit"]
    return [dict(row) for row in conn.execute(select(limits).join(links,
        links.c.resource_limit_id == limits.c.id).where(links.c.host_id == execution["host_id"],
        links.c.harness_id == execution["harness_id"], limits.c.kind == "provider_account")
        .order_by(limits.c.id).with_for_update(of=limits)).mappings()]


def _capacity(conn, execution):
    hh, requests, threads = tables["host_harness"], tables["provider_request"], tables["provider_thread"]
    config = conn.execute(select(hh).where(hh.c.host_id == execution["host_id"],
        hh.c.harness_id == execution["harness_id"], hh.c.enabled.is_(True))).mappings().first()
    if not config:
        raise DomainError("harness_unavailable", "The parent harness is disabled; no new reviewer may launch")
    active = conn.execute(select(func.count()).select_from(requests.join(threads,
        requests.c.provider_thread_id == threads.c.id)).where(requests.c.execution_id == execution["id"],
        threads.c.kind == "child", requests.c.status.in_(ACTIVE))).scalar_one()
    if active >= config["max_parallel_subagents"]:
        raise DomainError("review_capacity_unavailable", "Native reviewer capacity is occupied; reduce the batch or review directly")
    limits = provider_limits(conn, execution)
    claims = tables["resource_claim"]
    now = conn.execute(select(func.now())).scalar_one()
    for limit in limits:
        used = conn.execute(select(func.coalesce(func.sum(claims.c.units), 0)).where(
            claims.c.resource_limit_id == limit["id"], claims.c.released_at.is_(None))).scalar_one()
        maximum = 1 if limit["failure_count"] else limit["max_concurrent"]
        if used >= maximum or (limit["cooldown_until"] and limit["cooldown_until"] > now):
            raise DomainError("review_capacity_unavailable", "Shared provider capacity is occupied or cooling down; reduce the batch or review directly")
    return limits


def prepare(conn, actor, data: ReviewerPrepare, service):
    return _prepare(conn, actor, data, service, standalone=False)


def prepare_assignment(conn, actor, data: ReviewerPrepare, service):
    """Queue an independent review execution under the descriptor's pinned mode."""
    return _prepare(conn, actor, data, service, standalone=True)


def _assignment_result(assignment, artifact, obligation_id, manifest):
    return {"assignment": assignment, "manifest_artifact_id": artifact["id"],
            "obligation_id": obligation_id, "prompt": manifest["prompt"],
            "model_options": manifest["model_options"],
            "completion_condition": {"version": 1, "expression": {"op": "status_in",
                "target": {"kind": "assignment", "id": str(assignment["id"])},
                "values": ["completed", "failed", "cancelled"]}}}


def _work_key(item, descriptor_revision, policy_revision):
    return {"forge_item_id": item["id"], "head_commit_oid": item["head_commit_oid"],
            "reviewer_descriptor_revision_id": descriptor_revision,
            "policy_revision_id": policy_revision, "target_branch": item["target_branch"] or ""}


def _milestone_evidence(conn, item):
    from .milestones import head_check

    checked = head_check(conn, item)
    if checked is None:
        return None
    report = checked["report"]
    evidence = {"check_id": str(checked["id"]), "check_url": f"/api/v3/milestones/checks/{checked['id']}",
        "repository_id": str(checked["repository_id"]), "kind": checked["kind"],
        "status": "passed" if report.get("compiled") is True else "unconfirmed",
        "source_commit_oid": checked["source_commit_oid"], "base_commit_oid": checked["base_commit_oid"],
        "manifest_digest": report.get("manifest_digest"), "base_manifest_digest": report.get("base_manifest_digest"),
        "compiled": report.get("compiled"), "toolchain": report.get("toolchain")}
    jobs = tables["milestone_job"]
    job = conn.execute(select(jobs.c.id, jobs.c.status).where(jobs.c.check_id == checked["id"],
        jobs.c.project_id == checked["project_id"], jobs.c.status == "completed",
        jobs.c.request["source_commit_oid"].astext == checked["source_commit_oid"],
        jobs.c.request["base_commit_oid"].astext == checked["base_commit_oid"])
        .order_by(jobs.c.created_at.desc()).limit(1)).mappings().first()
    if job:
        evidence["job"] = {"id": str(job["id"]), "status": job["status"],
                           "url": f"/api/v3/milestones/verifications/{job['id']}"}
    return evidence


def _milestone_discovery(conn, item, project_id):
    query = urlencode({"view": "reviews", "expected_head_oid": item["head_commit_oid"], "limit": 1})
    discovery = {"url": f"/api/v3/forge-items/{item['id']}/inspect?{query}",
                 "check_id_field": "review_readiness.panel.check_id"}
    jobs, workspaces = tables["milestone_job"], tables["workspace"]
    job = conn.execute(select(jobs.c.id, jobs.c.status, jobs.c.request)
        .join(workspaces, jobs.c.workspace_id == workspaces.c.id).where(
            jobs.c.project_id == project_id, workspaces.c.repository_id == item["repository_id"],
            jobs.c.request["source_commit_oid"].astext == item["head_commit_oid"],
            jobs.c.request["solution_workspace_id"].astext.is_(None),
            jobs.c.status.in_(("queued", "running")))
        .order_by(jobs.c.created_at.desc()).limit(1)).mappings().first()
    if job:
        discovery["pending_job"] = {"id": str(job["id"]), "status": job["status"],
            "url": f"/api/v3/milestones/verifications/{job['id']}",
            "source_commit_oid": job["request"]["source_commit_oid"],
            "base_commit_oid": job["request"]["base_commit_oid"]}
    return discovery


def _existing_work(conn, service, execution, data, key, standalone):
    table = tables["review_work"]
    row = conn.execute(select(table).where(*(table.c[name] == value for name, value in key.items()))
        .with_for_update()).mappings().first()
    if row is None:
        if data.retry_of:
            raise DomainError("review_retry_changed", "The review contract changed; prepare its new head or rubric without retry_of")
        return None, 1
    kind = "assignment" if row["assignment_id"] else "provider_request"
    owner = get(conn, kind, row[kind + "_id"])
    if data.retry_of:
        if data.retry_of != owner["id"]:
            raise DomainError("revision_conflict", "The review owner changed; inspect its current attempt")
        if owner["status"] == "completed":
            from .reviews import _completed_review_defect
            item = get(conn, "forge_item", data.forge_item_id)
            descriptor = get(conn, "reviewer_descriptor", data.reviewer_descriptor_id)
            policy = get(conn, "review_policy", get(conn, "record_revision", key["policy_revision_id"])["content"]["id"])
            defect = _completed_review_defect(conn, item, policy, descriptor, owner, kind)
            if not defect or not defect["retryable"]:
                raise DomainError("review_result_not_retryable",
                    defect["reason"] if defect else "A completed substantive review cannot be retried to obtain another verdict",
                    report_defect=defect)
            if kind == "assignment":
                _, previous_manifest = assignment_manifest(conn, service, owner)
            else:
                artifact = get(conn, "artifact", owner["guidance_manifest_artifact_id"])
                previous_manifest = json.loads(service.store.read(artifact["content"]["sha256"]))
            previous_plan = previous_manifest.get("review_plan")
            if data.review_plan and data.review_plan.model_dump(mode="json") != previous_plan:
                raise DomainError("review_retry_changed", "A completed report repair must preserve its pinned review plan")
            if previous_plan:
                data.review_plan = ReviewPlan.model_validate(previous_plan)
        elif owner["status"] not in ("failed", "interrupted", "cancelled"):
            raise DomainError("review_unsettled", "Only a failed or stopped review may be explicitly retried")
        if kind == "assignment":
            executions = tables["execution"]
            if conn.execute(select(executions.c.id).where(executions.c.assignment_id == owner["id"],
                    executions.c.stop_confirmed_at.is_(None)).limit(1)).first():
                raise DomainError("review_unsettled", "Confirm the previous reviewer stopped before retrying")
        else:
            claims = tables["resource_claim"]
            if conn.execute(select(claims.c.id).where(claims.c.provider_request_id == owner["id"],
                    claims.c.released_at.is_(None)).limit(1)).first():
                raise DomainError("review_unsettled", "Reconcile the previous review's provider reservation before retrying")
        return None, row["attempts"] + 1
    if standalone != (kind == "assignment"):
        raise DomainError("review_already_owned", "This review already has an owner in another invocation mode", owner_id=str(owner["id"]))
    if kind == "assignment":
        artifact, manifest = assignment_manifest(conn, service, owner)
        return _assignment_result(owner, artifact, row["obligation_id"], manifest), row["attempts"]
    if owner["execution_id"] != execution["id"]:
        raise DomainError("review_already_owned", "Another maintainer owns this exact-head review; inspect its existing request",
                          owner_id=str(owner["id"]), owner_status=owner["status"])
    artifact = get(conn, "artifact", owner["guidance_manifest_artifact_id"])
    manifest = json.loads(service.store.read(artifact["content"]["sha256"]))
    return {"provider_request_id": owner["id"], "provider_thread_id": owner["provider_thread_id"],
            "status": owner["status"], "manifest_artifact_id": artifact["id"],
            "task_name": manifest["task_name"], "prompt": manifest["prompt"],
            "model_options": manifest["model_options"],
            "sandbox_manifest_artifact_id": execution["sandbox_manifest_artifact_id"]}, row["attempts"]


def _record_work(conn, key, attempts, obligation_id, **owner):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    table = tables["review_work"]
    values = {**key, "attempts": attempts, "obligation_id": obligation_id,
              "assignment_id": None, "provider_request_id": None, **owner}
    statement = pg_insert(table).values(**values)
    conn.execute(statement.on_conflict_do_update(index_elements=list(key), set_={
        name: value for name, value in values.items() if name not in key}))


def _prepare(conn, actor, data: ReviewerPrepare, service, *, standalone):
    execution = _maintainer(conn, actor)
    parent = get(conn, "provider_request", data.parent_request_id, lock=True)
    thread = get(conn, "provider_thread", parent["provider_thread_id"])
    if parent["execution_id"] != execution["id"] or thread["kind"] != "primary" or parent["status"] not in ("submitted", "running"):
        raise DomainError("invalid_review_parent", "Reviewer requires this maintainer's active primary request", 422)
    project_id = execution["project_id"]
    item = same_project(conn, "forge_item", data.forge_item_id, project_id)
    get(conn, "forge_item", item["id"], lock=True)
    repository = get(conn, "repository", item["repository_id"])
    descriptor = same_project(conn, "reviewer_descriptor", data.reviewer_descriptor_id, project_id)
    if item["status"] != "open":
        raise DomainError("forge_item_closed", "Review preparation requires an open Forge item")
    if item["kind"] != "pull_request":
        raise DomainError("review_requires_pull_request", "Prepare a PR review; use an ordinary scoped subagent and Forge discussion for an issue", 422)
    if item["kind"] == "pull_request" and (not data.expected_head_oid or data.expected_head_oid != item["head_commit_oid"]):
        raise DomainError("review_head_changed", "Refresh the pull request and pin its current head")
    if item["kind"] == "issue" and data.expected_head_oid is not None:
        raise DomainError("invalid_review_head", "Issues do not have a reviewed commit", 422)
    if not descriptor["enabled"]:
        raise DomainError("reviewer_disabled", "Reviewer descriptor is disabled")
    if standalone and descriptor["invocation"] != "assignment":
        raise DomainError("reviewer_requires_native", "This descriptor uses native subagents. Prepare a reviewer invocation; "
            "reconcile occupied child capacity or give a scoped maintainer ownership on a capable host. "
            "Standalone review requires an explicitly configured assignment descriptor.", 422)
    if not standalone and descriptor["invocation"] != "subrequest":
        raise DomainError("reviewer_needs_assignment", "This descriptor needs a separate assignment on its configured harness")
    if not standalone and descriptor["harness_id"] not in (None, execution["harness_id"]):
        raise DomainError("reviewer_harness_mismatch", "This native reviewer needs a maintainer on its configured harness; "
            "give a scoped maintainer on that harness ownership of the PR", 422)
    phase = item["review_phase"]
    if not phase:
        raise DomainError("review_phase_missing", "Classify the Forge item before selecting reviewers")
    policies, repos, reviewers = tables["review_policy"], tables["review_policy_repository"], tables["review_policy_reviewer"]
    matches = list(conn.execute(select(policies).join(repos, repos.c.review_policy_id == policies.c.id).where(
        repos.c.repository_id == repository["id"], policies.c.phases.contains([phase]), policies.c.enabled.is_(True))).mappings())
    if len(matches) != 1:
        raise DomainError("review_policy_unavailable", "Repository and phase need exactly one enabled review policy")
    policy = dict(matches[0])
    identity_id = descriptor["integration_identity_id"] or policy["maintainer_identity_id"]
    if identity_id:
        identity = get(conn, "integration_identity", identity_id)
        if not identity["enabled"] or identity["integration_id"] != repository["integration_id"]:
            raise DomainError("review_identity_mismatch", "Reviewer Forge identity is disabled or belongs to another integration", 422)
    bundle = get(conn, "artifact", thread["skill_bundle_artifact_id"])
    catalog = json.loads(service.store.read(bundle["content"]["sha256"]))
    functions = descriptor["functions"]
    if any(function not in catalog["functions"] for function in functions):
        raise DomainError("reviewer_skill_unavailable", "Descriptor functions must exist in the parent's pinned skill bundle", 422)
    descriptor_revision = snapshot(conn, "reviewer_descriptor", descriptor, actor.id)
    policy_revision = snapshot(conn, "review_policy", policy, actor.id)
    work_key = _work_key(item, descriptor_revision, policy_revision)
    existing, attempts = _existing_work(conn, service, execution, data, work_key, standalone)
    if existing is not None:
        return existing
    limits = [] if standalone else _capacity(conn, execution)
    guidance, documents = tables["reviewer_guidance"], tables["document"]
    sources = [dict(row) for row in conn.execute(select(documents).join(guidance,
        guidance.c.document_id == documents.c.id).where(guidance.c.reviewer_descriptor_id == descriptor["id"])
        .order_by(documents.c.id)).mappings()]
    for document in sources:
        if document["project_id"] != project_id:
            raise DomainError("scope_mismatch", "Reviewer guidance belongs to another project", 422)
    harness_id = descriptor["harness_id"] or execution["harness_id"]
    harness = get(conn, "harness", harness_id)
    harness_revision = thread["harness_revision_id"] if harness_id == execution["harness_id"] else snapshot(conn, "harness", harness, actor.id)
    base_options = thread["applied_model_options"] if harness_id == execution["harness_id"] else harness["model_options"]
    options = ModelOptions.model_validate({**base_options, **descriptor["model_options"]}).model_dump()
    request_id = uuid4()
    label = "hz_review_" + request_id.hex
    milestone_evidence = _milestone_evidence(conn, item)
    milestone_discovery = _milestone_discovery(conn, item, project_id)
    from .review_packets import encode, prepare_packet
    report_request_id = "$HORIZON_PROVIDER_REQUEST_ID" if standalone else str(request_id)
    packet, context_artifact = prepare_packet(conn, service, project_id=project_id, execution_id=execution["id"],
        item=item, repository=repository, descriptor=descriptor, descriptor_revision=descriptor_revision,
        policy=policy, policy_revision=policy_revision,
        functions={function: catalog["functions"][function] for function in functions},
        guidance=sources, instructions=data.instructions, report_schema=ReviewerReport.model_json_schema(),
        milestone_evidence=milestone_evidence, milestone_discovery=milestone_discovery,
        report_request_id=report_request_id,
        review_plan=data.review_plan.model_dump(mode="json") if data.review_plan else None)
    inspect_query = urlencode({"view": "diff" if item["kind"] == "pull_request" else "comments",
        **({"expected_head_oid": data.expected_head_oid} if data.expected_head_oid else {})})
    prompt = "\n\n".join([
        "Review the pinned Forge item in a separate context. Return precise findings for this head. "
        "Your review is advisory; the maintainer decides changes and merging. Respect the execution's sandbox and tool restrictions. "
        "Publish your own scoped review under your reviewer account; do not merge or modify the shared branch. Identify any blocker and its evidence.",
        f"Review label: {label}", f"Repository: {repository['slug']}; Forge item: {item['remote_number']}; phase: {phase}",
        f"Horizon project UUID: {project_id}; repository UUID: {repository['id']}; Forge item UUID: {item['id']}. "
        "Use these UUIDs in Horizon API routes and project filters.",
        f"Pinned item inspection: GET /api/v3/forge-items/{item['id']}/inspect?{inspect_query}. "
        "Use the same item UUID and expected_head_oid with view=files, comments, reviews or review_comments as needed. "
        "The repository-local Forge item number above is a display reference.",
        f"Reviewer: {descriptor['slug']}; rubric revision: {descriptor['revision']}",
        f"Pinned head: {data.expected_head_oid or 'issue discussion'}; target branch: {item['target_branch']}",
        "Exact-head review packet:\n" + encode(packet),
        "Start with the packet's selected rubric, policy and source links. It already supplies the reporting contract "
        "and verification summary: do not repeat catalog, skill or schema discovery for material included here. "
        "Read any explicitly omitted instructions from complete_context before deciding, selecting their JSON pointer "
        "instead of printing the whole artifact. Other focused skills and deeper evidence are available when a concrete "
        "review question requires them. The complete artifact preserves the exact policy, descriptor, function instructions, "
        "guidance and report schema; no omitted requirement is waived. "
        "This preparation snapshot may precede the trusted result. Refresh discovery at a useful review boundary; "
        "when review_readiness.panel.check_id is present, fetch GET /api/v3/milestones/checks/{check_id}. "
        "A pending job URL reports status and its eventual check_id; job IDs and check IDs are distinct. "
        "Confirm the repository, source/base commits, manifest and toolchain apply to this comparison before reusing a receipt. "
        "Do not duplicate a full project or dependency build while matching trusted verification is pending or passed, "
        "unless a specific concern needs evidence outside its coverage. Identify that concern first and prefer a focused probe. "
        "Continue semantic and source assessment while verification runs; missing or passed build evidence does not decide semantic quality.",
        "Run any necessary probe only in scratch you create under the inherited disk-backed $TMPDIR "
        '(for example, mktemp -d "$TMPDIR/horizon-review.XXXXXX"). Do not hard-code /tmp. '
        "Host-managed lean-build directories, verification job checkouts and worker caches are not your scratch workspace: "
        "do not enter them to run builds, edit files, copy probes there or recreate deleted job directories. "
        "Use published verification receipts; ask the maintainer for a task-owned environment if a focused probe lacks dependencies.",
        *([
            "The check receipt and verification job have distinct IDs and URLs. Reuse the receipt only for its "
            "recorded repository, source/base commits, manifest and toolchain; confirm those pins apply to the reviewed comparison. "
            "A passed receipt does not establish semantic correctness. For a route-classified receipt, assess decomposition, "
            "dependency order and bridge obligations. Compile a contract or project during route review only for a concrete "
            "concern not already checked at applicable pins; cite that concern and use a focused check."] if milestone_evidence else []),
        "Return your verdict with a concise evidence-based summary, or a typed assessment using the artifact's "
        "report_schema when structured dimensions, findings or resolutions are needed. "
        "When report.assessment_required_for_approval is true, approval requires the typed assessment; "
        "follow approval_requirements and use assessment_template when present, otherwise typed_schema. "
        "Only your own prior change requests can be resolved; independently inspect the current repair before doing so. "
        "An approval never silently withdraws an earlier objection. Include optional line findings with path, body, and exactly one "
        "positive new_position or old_position (file line number on the reviewed diff side). "
        "Publish these yourself through POST /api/v3/reviewer-invocations/" +
        report_request_id + "/report "
        "with {verdict, assessment, comments, historical:false} (or summary instead of assessment), using the inherited Horizon execution credential. "
        "The server uses your descriptor's Forge account. Verify delivery with GET /api/v3/operations/"
        "{response.idempotency_key}?operation=reviewer_report: use the returned idempotency_key, not id "
        "(the outbox UUID), and reviewer_report, not the response.kind forge_review. "
        "The receipt must have status=completed and result_ref_id. Return its link to the maintainer. "
        + ("Then finish your independent reviewer assignment after settling its own mission and obligations. "
         if standalone else "Then finish your native review turn. ") +
        "Do not wait for your own approval/readiness gate: "
        "independent coverage becomes eligible only after the provider records your actual completion. "
        "A delivered report does not release your native capacity while you are still working. "
        "If the head/base changed, use historical:true and verdict:commented, with no inline coordinates. "
        "Keep credentials out of messages and tool output.",
        "Only if the included reporting contract does not cover your intended submission, consult the pinned "
        "report_schema or `horizon-pipeline agent schema --section operations --name reviewer_report`. "
        "Submit with `horizon-pipeline agent request POST /api/v3/reviewer-invocations/" +
        report_request_id + "/report JSON`; this client supplies "
        "the required Idempotency-Key and journals uncertain writes. A minimal commented-feedback body is "
        '{"verdict":"commented","summary":"Your assessment and evidence","historical":false}; replace the summary '
        "with your actual findings. This example is not an approval template. Choose the justified verdict "
        "and satisfy its reporting requirements. A typed assessment is required for planned approval and "
        "explicit prior-objection resolutions; do not invent its field shapes. "
        "For long bodies use the same request command with --body-file PATH instead of inline JSON.",
        "Read the relevant PR reviews, inline findings and replies at the packet's exact links; follow next_page only "
        "while more relevant evidence remains. Inline comments use the remote review number from reviews.items[].id. "
        "Link unresolved findings and explain what remains valid after changes; do not rediscover or erase previous work. "
        "Lead with the conclusion, then scope, material findings, evidence and limits. Keep hashes and internal IDs out of the narrative. "
        "Publish the complete assessment yourself, not only a private handoff; the maintainer checks its delivery. "
        "retargeted, superseded or incomplete findings use historical commented feedback, never stale approval.",
    ])
    if data.review_plan:
        prompt += ("\n\nThe packet's shadow review plan does not waive the current repository policy or required panel. "
            "Independently confirm or challenge this scope and risk classification. "
            "Assess the selected dimensions explicitly; report unanticipated impact and broaden review when necessary. "
            "Approval requires assessment.classification_confirmed=true and assessment.dimensions covering every "
            "selected question, along with a complete evidence-based assessment. A summary alone cannot approve this plan.")
    if data.retry_of:
        prompt += ("\n\nDiagnosed recovery context from the maintainer (quoted input, not a verdict or extra authority): "
            + json.dumps(data.retry_note[:1200], ensure_ascii=False)
            + "\nReuse applicable evidence at the pinned head, policy and review scope. For a reporting defect, "
            "focus on completing the missing assessment, explicit resolutions or provenance. Independently verify "
            "the evidence and choose the justified verdict; the recovery request does not establish approval. "
            "The full retry note is preserved at GET /api/v3/reviewer-invocations/" + report_request_id
            + " with JSON pointer /manifest/retry_note.")
    manifest = {"schema_version": 1, "kind": "reviewer_invocation", "provider_request_id": request_id,
        "parent_request_id": parent["id"], "execution_id": execution["id"], "assignment_id": execution["assignment_id"],
        "forge_item_id": item["id"], "repository_id": repository["id"], "integration_id": repository["integration_id"],
        "forge_item_revision": item["revision"], "head_commit_oid": data.expected_head_oid, "review_phase": phase,
        "target_branch": item["target_branch"],
        "policy_id": policy["id"], "policy_revision_id": policy_revision, "reviewer_descriptor_id": descriptor["id"],
        "reviewer_descriptor_revision_id": descriptor_revision, "integration_identity_id": identity_id,
        "harness_revision_id": harness_revision, "harness_id": harness_id, "skill_bundle_artifact_id": thread["skill_bundle_artifact_id"],
        "sandbox_manifest_artifact_id": execution["sandbox_manifest_artifact_id"], "guidance": sources,
        "functions": functions, "model_options": options, "task_name": label, "prompt": prompt,
        "milestone_discovery": milestone_discovery, "review_packet": packet,
        "review_context_artifact_id": context_artifact["id"]}
    if milestone_evidence:
        manifest["milestone_evidence"] = milestone_evidence
    if data.review_plan:
        manifest["review_plan"] = data.review_plan.model_dump(mode="json")
    if data.retry_of:
        manifest["retry_of"] = str(data.retry_of)
        manifest["retry_note"] = data.retry_note
    if standalone:
        delegator = get(conn, "assignment", execution["assignment_id"])
        parent_mission = get(conn, "mission", delegator["mission_id"])
        mission = service.mission(conn, actor, MissionCreate(project_id=project_id, parent_id=parent_mission["id"],
            expected_parent_revision=parent_mission["revision"],
            acceptance_criteria=["Deliver an exact-head reviewer assessment and verified Forge receipt"],
            delegation_note="Independently assess this PR against one pinned specialist rubric",
            roadmap_document_id=parent_mission["roadmap_document_id"],
            title=f"{descriptor['slug']}: review {repository['slug']} #{item['remote_number']}",
            objective=f"Review Forge item {item['id']} at {data.expected_head_oid or 'the pinned issue revision'} "
                      "against the pinned rubric; record precise findings and evidence for the maintainer's decision."))
        assignment = service.assignment(conn, actor, AssignmentCreate(run_id=delegator["run_id"], mission_id=mission["id"],
            parent_id=delegator["id"], reviewer_descriptor_id=descriptor["id"], role="worker", functions=functions,
            instructions="Follow the immutable reviewer manifest. Publish your assessment through your invocation's /report endpoint, "
                         "verify delivery under your reviewer identity, settle your own deliverable obligation with the receipt, "
                         "complete this narrow review mission, then return the review link. Do not await PR acceptance or author repairs.",
            harness_id=harness_id, model_options=ModelOptions.model_validate(options)), repair=True, internal=True)
        prompt += ("\n\nDurable reviewer lifecycle: this independent assignment can finish even if its dispatching maintainer "
            "has already exited. Start from the pinned review packet above; no repeated general startup discovery is needed. "
            f"Your assignment UUID is {assignment['id']}; your narrow review mission UUID is {mission['id']}. "
            "Use your own current $HORIZON_PROVIDER_REQUEST_ID in the report route. After verifying the delivered review receipt, "
            "read your own ledger with `horizon-pipeline agent context`, then resolve its deliverable obligation through "
            "POST /api/v3/obligations/{id}/resolve using its expected_revision, status:done, and "
            "resolution:{kind:completed,note:YOUR_RESULT_AND_VERIFIED_RECEIPT_URL,"
            "evidence:[{kind:provider_request,id:YOUR_CURRENT_HORIZON_PROVIDER_REQUEST_ID}]}. "
            "Use the actual request UUID from your environment; no separate Forge-review UUID lookup is needed. "
            f"Read GET /api/v3/records/mission/{mission['id']} for the current revision, then POST /api/v3/commands with "
            f'{{"operation":"complete_mission","target_id":"{mission["id"]}","expected_revision":CURRENT_REVISION,'
            '"args":{"note":"YOUR_DELIVERED_REVIEW_AND_RECEIPT"}}. '
            "Settle any other actual obligations and delivery failures, then return your verdict, concise findings and review link. "
            "A completed changes-request assessment fulfills this review mission; repairs and acceptance belong to integration. "
            "Do not wait for the PR to merge, an author to reply, or your own review_readiness gate, which requires your actual "
            "provider completion. If the assessment or delivery remains incomplete, record its blocker and concrete recovery owner "
            "instead of claiming completion.")
        manifest["prompt"] = prompt
        manifest.update(kind="reviewer_assignment", assignment_id=assignment["id"],
                        parent_assignment_id=delegator["id"], parent_execution_id=execution["id"])
        manifest.pop("provider_request_id")
        manifest.pop("execution_id")
        artifact = save_blob(conn, service.store, project_id, manifest, execution["id"])
        conn.execute(insert(tables["assignment_artifact"]).values(assignment_id=assignment["id"],
            artifact_id=artifact["id"], purpose="reviewer_manifest"))
        obligation = create(conn, "obligation", assignment_id=delegator["id"], created_by_execution_id=execution["id"],
            number=next_number(conn, "obligation", "assignment_id", delegator["id"]), kind="review",
            description=f"Publish {descriptor['slug']} findings from assignment {assignment['id']} on the PR and verify delivery "
                        f"on Forge item {item['remote_number']} at {data.expected_head_oid or 'the pinned issue revision'}. "
                        "Delegate this obligation explicitly before yielding; it is not resolved merely by queueing the reviewer.")
        emit(conn, actor.id, project_id, "assignment", assignment, ["reviewer_manifest"], execution_id=execution["id"])
        _record_work(conn, work_key, attempts, obligation["id"], assignment_id=assignment["id"])
        from .review_labels import queue_state
        queue_state(conn, service, actor.id, item, descriptor, "queued", str(assignment["id"]))
        return _assignment_result(assignment, artifact, obligation["id"], manifest)
    artifact = save_blob(conn, service.store, project_id, manifest, execution["id"])
    child = create(conn, "provider_thread", assignment_id=thread["assignment_id"],
        number=next_number(conn, "provider_thread", "assignment_id", thread["assignment_id"]), kind="child",
        parent_request_id=parent["id"], workspace_id=thread["workspace_id"], harness_revision_id=thread["harness_revision_id"],
        skill_bundle_artifact_id=thread["skill_bundle_artifact_id"], provider_state_ref=f"prepared-review:{request_id}",
        applied_model_options=options)
    request = create(conn, "provider_request", id=request_id, provider_thread_id=child["id"], execution_id=execution["id"],
        number=1, reason="review", reviewer_descriptor_id=descriptor["id"], reviewer_descriptor_revision_id=descriptor_revision,
        guidance_manifest_artifact_id=artifact["id"], input_artifact_id=artifact["id"], status="pending")
    for limit in limits:
        create(conn, "resource_claim", resource_limit_id=limit["id"], execution_id=execution["id"],
               provider_request_id=request["id"], units=1)
    obligation = create(conn, "obligation", assignment_id=execution["assignment_id"], created_by_execution_id=execution["id"],
        number=next_number(conn, "obligation", "assignment_id", execution["assignment_id"]), kind="review",
        description=f"Publish reviewer {descriptor['slug']} invocation {request_id} findings on Forge item {item['remote_number']} "
                    f"at {data.expected_head_oid or 'the pinned issue revision'} and verify delivery. "
                    "Preserve historical/incomplete findings as comments; record an explicit cancellation reason if no assessment exists.")
    _record_work(conn, work_key, attempts, obligation["id"], provider_request_id=request["id"])
    emit(conn, actor.id, project_id, "provider_request", request, ["status", "reviewer_descriptor_revision_id"], execution_id=execution["id"])
    from .review_labels import queue_state
    queue_state(conn, service, actor.id, item, descriptor, "running", str(request["id"]), request)
    return {"provider_request_id": request["id"], "provider_thread_id": child["id"], "status": request["status"],
            "manifest_artifact_id": artifact["id"], "task_name": label, "prompt": prompt, "model_options": options,
            "sandbox_manifest_artifact_id": execution["sandbox_manifest_artifact_id"]}


def assignment_manifest(conn, service, assignment):
    """Return the pinned review manifest without consulting mutable descriptors."""
    link, artifact = tables["assignment_artifact"], tables["artifact"]
    row = conn.execute(select(artifact).join(link, link.c.artifact_id == artifact.c.id).where(
        link.c.assignment_id == assignment["id"], link.c.purpose == "reviewer_manifest")).mappings().first()
    if not row:
        if assignment["reviewer_descriptor_id"]:
            raise DomainError("review_manifest_missing", "Reviewer assignments must be prepared by a live maintainer")
        return None
    manifest = json.loads(service.store.read(row["content"]["sha256"]))
    if manifest.get("kind") != "reviewer_assignment" or manifest.get("assignment_id") != str(assignment["id"]) or (
            manifest.get("reviewer_descriptor_id") != str(assignment["reviewer_descriptor_id"])):
        raise DomainError("review_manifest_mismatch", "Assignment reviewer provenance does not match its immutable manifest")
    return dict(row), manifest


def assignment_request_fields(conn, service, assignment):
    pinned = assignment_manifest(conn, service, assignment)
    if not pinned:
        return {}
    artifact, manifest = pinned
    return {"reason": "review", "reviewer_descriptor_id": UUID(manifest["reviewer_descriptor_id"]),
            "reviewer_descriptor_revision_id": UUID(manifest["reviewer_descriptor_revision_id"]),
            "guidance_manifest_artifact_id": artifact["id"]}


def read(conn, actor, request_id, service):
    request = get(conn, "provider_request", request_id)
    project_id = project_of(conn, "provider_request", request_id)
    require_project(conn, actor, project_id)
    if request["reason"] != "review" or not request["guidance_manifest_artifact_id"]:
        raise DomainError("not_review_invocation", "Request is not a prepared review invocation", 422)
    artifact = get(conn, "artifact", request["guidance_manifest_artifact_id"])
    manifest = json.loads(service.store.read(artifact["content"]["sha256"]))
    return {"request": request, "thread": get(conn, "provider_thread", request["provider_thread_id"]), "manifest": manifest}


def attach(conn, actor, request_id, data: ReviewerAttach):
    execution, request, thread = _owned(conn, actor, request_id)
    parent = get(conn, "provider_request", thread["parent_request_id"])
    parent_thread = get(conn, "provider_thread", parent["provider_thread_id"])
    harness = get(conn, "record_revision", thread["harness_revision_id"])
    adapter = harness["content"]["adapter"].removesuffix("_exec")
    result = bind_native(conn, execution, parent_thread, request["id"], adapter, data.native_key,
                         data.native_invocation_id, data.background)
    emit(conn, actor.id, execution["project_id"], "provider_request", result[1], ["status"], execution_id=execution["id"])
    return {"provider_request_id": request["id"], "provider_thread_id": result[0]["id"], "status": result[1]["status"]}


def bind_native(conn, execution, parent_thread, request_id, adapter, native_key, invocation_id=None, background=None):
    """Attach a native observation only to an already authorized invocation."""
    from .provider_events import _invocation_ref, child_state_ref

    if adapter == "codex" and _invocation_ref(native_key.rsplit("/", 1)[-1]):
        raise DomainError("invalid_arguments", "native_key must be the actual child ID returned by spawn_agent "
            "(receiver_thread_id), not the hz_review task label. Launch with the complete prepared prompt, "
            "then attach that returned child ID; attach does not launch a reviewer.", 422)
    request = get(conn, "provider_request", request_id, lock=True)
    child = get(conn, "provider_thread", request["provider_thread_id"], lock=True)
    parent = get(conn, "provider_request", child["parent_request_id"])
    if request["execution_id"] != execution["id"] or request["reason"] != "review" or parent["provider_thread_id"] != parent_thread["id"]:
        raise DomainError("scope_mismatch", "Native review binding belongs to another request", 422)
    state_ref = child_state_ref(parent_thread["id"], adapter, native_key)
    native_id = native_key if adapter == "codex" else None
    if child["provider_state_ref"] != f"prepared-review:{request_id}" and (
            child["provider_state_ref"] != state_ref or child["provider_thread_id"] != native_id):
        raise DomainError("review_identity_changed", "Review invocation is already attached to another native child")
    if request["status"] in TERMINAL and child["provider_state_ref"].startswith("prepared-review:"):
        raise DomainError("review_cancelled", "An unlaunched cancelled reviewer cannot be attached")
    threads = tables["provider_thread"]
    identity = threads.c.provider_state_ref == state_ref
    if native_id:
        identity = identity | (threads.c.provider_thread_id == native_id)
    others = list(conn.execute(select(threads).where(threads.c.assignment_id == child["assignment_id"],
        threads.c.id != child["id"], identity).with_for_update()).mappings())
    other = dict(others[0]) if len(others) == 1 else None
    if others and other is None:
        raise DomainError("review_identity_conflict", "Native child identity has multiple observed records; reconcile explicitly")
    if other:
        child, request = _bind_observed_child(conn, execution, request, child, other, native_id, invocation_id)
    if child["provider_state_ref"] != state_ref:
        child = change(conn, "provider_thread", child["id"], provider_state_ref=state_ref, provider_thread_id=native_id, status="available")
    values = {}
    if request["status"] == "pending":
        values.update(status="submitted", submitted_at=conn.execute(select(func.now())).scalar_one())
    if invocation_id and not request["provider_turn_id"]:
        values["provider_turn_id"] = invocation_id
    elif invocation_id and request["provider_turn_id"] != invocation_id:
        raise DomainError("review_identity_changed", "Review invocation is already bound to another native call")
    if background is not None and request["native_background"] is None:
        values["native_background"] = background
    if values:
        request = change(conn, "provider_request", request["id"], **values)
    return child, request


def _bind_observed_child(conn, execution, prepared, placeholder, observed, native_id, invocation_id):
    """Retain duplicate history while binding its identity to the prepared review."""
    requests = tables["provider_request"]
    rows = list(conn.execute(select(requests).where(requests.c.provider_thread_id == observed["id"])
        .with_for_update()).mappings())
    original = dict(rows[0]) if len(rows) == 1 else None
    compatible = (placeholder["provider_state_ref"] == f"prepared-review:{prepared['id']}"
        and prepared["status"] == "pending" and prepared["provider_turn_id"] is None
        and observed["kind"] == "child" and observed["provider_thread_id"] == native_id
        and all(observed[key] == placeholder[key] for key in (
            "parent_request_id", "workspace_id", "harness_revision_id", "skill_bundle_artifact_id"))
        and original is not None and original["execution_id"] == execution["id"]
        and original["reason"] == "continuation" and original["status"] == "running"
        and not any(original[key] for key in (
            "reviewer_descriptor_id", "reviewer_descriptor_revision_id", "guidance_manifest_artifact_id", "input_artifact_id"))
        and original["created_at"] >= prepared["created_at"]
        and (invocation_id is None or invocation_id == original["provider_turn_id"]))
    if not compatible:
        raise DomainError("review_identity_conflict", "Native child does not match one active unregistered invocation "
            "of this pending review in the same execution and parent request; preserve its history and reconcile explicitly")
    now = conn.execute(select(func.now())).scalar_one()
    note = f"Duplicate native observation tracking reconciled into prepared review {prepared['id']}; the native child continues."
    change(conn, "provider_request", original["id"], status="interrupted", finished_at=now,
        failure={"kind": "execution", "code": "review_binding_reconciled", "message": note})
    release_child_claims(conn, original["id"], now)
    request = change(conn, "provider_request", prepared["id"],
        status=original["status"], submitted_at=original["submitted_at"], started_at=original["started_at"],
        provider_turn_id=original["provider_turn_id"], native_background=original["native_background"])
    change(conn, "provider_thread", observed["id"], status="closed", provider_thread_id=None,
        provider_state_ref=f"reconciled-review:{placeholder['id']}",
        recovery_note=f"Native child {native_id} is tracked by prepared review {prepared['id']} on thread {placeholder['id']}.")
    child = change(conn, "provider_thread", placeholder["id"], label="hz_review_" + prepared["id"].hex,
        recovery_note=f"Native child {native_id} was first observed on thread {observed['id']}, request {original['id']}.")
    create(conn, "activity", assignment_id=execution["assignment_id"], execution_id=execution["id"],
        provider_thread_id=child["id"], provider_request_id=request["id"], kind="progress",
        summary=note, occurred_at=now)
    return child, request


def cancel(conn, actor, request_id, data: ReviewerCancel, *, service=None):
    execution, request, thread = _owned(conn, actor, request_id)
    if request["status"] in TERMINAL:
        return {"provider_request_id": request["id"], "status": request["status"], "stop_required": False}
    now = conn.execute(select(func.now())).scalar_one()
    unlaunched = request["status"] == "pending" and thread["provider_state_ref"].startswith("prepared-review:")
    values = {"status": "interrupted" if unlaunched else "uncertain",
              "failure": {"kind": "execution", "code": "review_cancel_requested", "message": data.note}}
    if unlaunched:
        values["finished_at"] = now
        release_child_claims(conn, request_id, now)
        change(conn, "provider_thread", thread["id"], status="closed")
    request = change(conn, "provider_request", request["id"], **values)
    emit(conn, actor.id, execution["project_id"], "provider_request", request, ["status", "failure"], execution_id=execution["id"])
    if service is not None:
        from .review_labels import invocation_state
        invocation_state(conn, service, actor.id, request, "cancelled", str(request_id) + ":cancel")
    return {"provider_request_id": request["id"], "status": request["status"], "stop_required": not unlaunched}


def release_child_claims(conn, request_id, observed):
    claims = tables["resource_claim"]
    conn.execute(update(claims).where(claims.c.provider_request_id == request_id,
        claims.c.released_at.is_(None)).values(released_at=observed))


def account_observed_child(conn, execution, request, observed):
    """Unregistered observed children still consume actual shared provider capacity."""
    if request["status"] in TERMINAL:
        release_child_claims(conn, request["id"], observed)
        return
    claims = tables["resource_claim"]
    for limit in provider_limits(conn, execution):
        if not conn.execute(select(claims.c.id).where(claims.c.provider_request_id == request["id"],
                claims.c.resource_limit_id == limit["id"], claims.c.released_at.is_(None))).first():
            create(conn, "resource_claim", resource_limit_id=limit["id"], execution_id=execution["id"],
                   provider_request_id=request["id"], units=1)
