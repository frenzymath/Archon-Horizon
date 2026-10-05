"""Maintainer-selected, revision-pinned Forge decisions and review invocations."""

import json
import re
from uuid import UUID

from sqlalchemy import select
from pydantic import ValidationError

from .auth import is_admin, live_execution, require_project
from .errors import DomainError
from .models import ForgeLabel, ForgeMerge, ForgeReview, ReviewCarryForwardEvidence
from .records import create, get, project_of, same_project, snapshot
from .schema import tables


def _review_history(conn, item):
    review, operation, reference = (tables[name] for name in
        ("forge_review", "outbox_operation", "object_reference"))
    rows = list(conn.execute(select(review).where(review.c.forge_item_id == item["id"])
        .order_by(review.c.observed_at.desc(), review.c.created_at.desc(), review.c.id.desc())).mappings())
    payloads = {row.forge_review_id: row.payload for row in conn.execute(
        select(reference.c.forge_review_id, operation.c.payload)
        .join(reference, reference.c.id == operation.c.result_ref_id)
        .join(review, review.c.id == reference.c.forge_review_id).where(
            review.c.forge_item_id == item["id"], operation.c.kind == "forge_review",
            operation.c.status == "completed"))}
    return rows, payloads


def _current_approval(conn, row, payload, item, policy, *, carried=False):
    if (not payload or row["verdict"] != "approved" or (not carried and row["commit_oid"] != item["head_commit_oid"])
            or not row["provider_request_id"] or not row["reviewer_descriptor_revision_id"]
            or "**Historical reviewer feedback:**" in row["summary"]):
        return False
    descriptor = get(conn, "reviewer_descriptor", row["reviewer_descriptor_id"])
    revision = get(conn, "record_revision", row["reviewer_descriptor_revision_id"])
    request = get(conn, "provider_request", row["provider_request_id"])
    if (not descriptor["enabled"] or revision["object_revision"] != descriptor["revision"]
            or request["status"] != "completed" or request["reason"] != "review"
            or request["reviewer_descriptor_id"] != descriptor["id"]
            or request["reviewer_descriptor_revision_id"] != revision["id"]
            or not request["guidance_manifest_artifact_id"] or not payload.get("policy_revision_id")):
        return False
    if payload:
        policy_revision = get(conn, "record_revision", payload["policy_revision_id"])
        if (payload.get("historical") or payload.get("target_branch") != item["target_branch"]
                or str(policy_revision["content"].get("id")) != str(policy["id"])
                or policy_revision["object_revision"] != policy["revision"]
                or (payload.get("review_plan") and payload.get("review_base_verified") is not True)):
            return False
    return True


def _completed_review_defect(conn, item, policy, descriptor, owner, kind):
    """Identify reporting defects without offering a new vote on a substantive decision."""
    request, execution, operation = (tables[name] for name in
        ("provider_request", "execution", "outbox_operation"))
    owned_requests = select(request.c.id).where(request.c.reason == "review",
        request.c.reviewer_descriptor_id == descriptor["id"])
    if kind == "assignment":
        owned_requests = owned_requests.join(execution, request.c.execution_id == execution.c.id).where(
            execution.c.assignment_id == owner["id"])
    else:
        owned_requests = owned_requests.where(request.c.id == owner["id"])
    request_ids = set(conn.execute(owned_requests).scalars())
    pending = conn.execute(select(operation.c.id).where(operation.c.kind == "forge_review",
        operation.c.payload["provider_request_id"].astext.in_([str(value) for value in request_ids]),
        operation.c.status.in_(("pending", "running", "uncertain", "failed"))).limit(1)).first()
    if pending:
        return {"code": "review_delivery_unsettled", "retryable": False,
                "operation_id": str(pending.id),
                "reason": "Reconcile the existing report delivery before preparing another reviewer."}
    rows, payloads = _review_history(conn, item)
    owned = [row for row in rows if row["provider_request_id"] in request_ids
        and row["reviewer_descriptor_id"] == descriptor["id"]
        and row["commit_oid"] == item["head_commit_oid"]]
    if not owned:
        return {"code": "review_report_missing", "retryable": True,
                "reason": "The completed reviewer has no delivered report on its pinned head."}
    row = owned[0]
    payload = payloads.get(row["id"], {})
    earlier_adverse = any(previous["verdict"] == "changes_requested" for previous in owned[1:])
    if row["verdict"] == "changes_requested" or (earlier_adverse and (
            row["verdict"] != "approved" or not payload.get("reviewer_submission")
            or payload.get("provider_request_id") != str(row["provider_request_id"]))):
        return None
    defect = {"review_id": str(row["id"]), "retryable": True}
    if row["verdict"] != "approved":
        return {**defect, "code": "review_report_incomplete",
                "reason": "The completed reviewer delivered feedback without a completed assessment."}
    if not payload.get("reviewer_submission") or not _current_approval(conn, row, payload, item, policy):
        return {**defect, "code": "invalid_specialist_provenance",
                "reason": "The delivered approval lacks current independent reviewer provenance."}
    assessment = payload.get("assessment") or {}
    if assessment:
        from .review_contracts import ReviewAssessment
        try:
            ReviewAssessment.model_validate(assessment).check_verdict("approved")
        except ValueError:
            return {**defect, "code": "review_assessment_incomplete",
                    "reason": "The delivered approval has an incomplete or invalid typed assessment."}
    if payload.get("review_plan") and (not assessment.get("classification_confirmed") or
            not {question["dimension"] for question in payload["review_plan"]["questions"]}.issubset(
                assessment.get("dimensions", []))):
        return {**defect, "code": "review_plan_incomplete",
                "reason": "The delivered approval did not complete its pinned review plan."}
    resolved = {entry["review_id"] for entry in assessment.get("resolutions", [])}
    missing = [str(previous["id"]) for previous in rows
        if previous["reviewer_descriptor_id"] == descriptor["id"]
        and previous["verdict"] == "changes_requested" and str(previous["id"]) not in resolved]
    if missing:
        return {**defect, "code": "review_resolutions_required", "review_ids": missing,
                "reason": "The delivered approval omitted explicit resolution of this reviewer's earlier objections."}
    return None


def _review_next_action(conn, item, policy, descriptor, dimensions):
    """Describe the existing owner before proposing a reservation or explicit retry."""
    standalone = descriptor["invocation"] == "assignment"
    action = {"action": "prepare_reviewer_assignment" if standalone else "prepare_reviewer_invocation",
        "method": "POST", "endpoint": "/api/v3/reviewer-assignments" if standalone else "/api/v3/reviewer-invocations",
        "reviewer_descriptor_id": str(descriptor["id"]), "dimensions": dimensions,
        "forge_item_id": str(item["id"]), "expected_head_oid": item["head_commit_oid"],
        "required_fields": ["parent_request_id"]}
    work = tables["review_work"]
    descriptor_revision = tables["record_revision"].alias("descriptor_revision")
    policy_revision = tables["record_revision"].alias("policy_revision")
    current = conn.execute(select(work).join(descriptor_revision,
        descriptor_revision.c.id == work.c.reviewer_descriptor_revision_id).join(policy_revision,
        policy_revision.c.id == work.c.policy_revision_id).where(
            work.c.forge_item_id == item["id"], work.c.head_commit_oid == item["head_commit_oid"],
            work.c.target_branch == (item["target_branch"] or ""),
            descriptor_revision.c.content["id"].astext == str(descriptor["id"]),
            descriptor_revision.c.object_revision == descriptor["revision"],
            policy_revision.c.content["id"].astext == str(policy["id"]),
            policy_revision.c.object_revision == policy["revision"])).mappings().first()
    if current is None:
        return action, None
    kind = "assignment" if current["assignment_id"] else "provider_request"
    owner = get(conn, kind, current[kind + "_id"])
    action.update(owner_id=str(owner["id"]), owner_kind=kind, owner_status=owner["status"],
                  attempts=current["attempts"])
    if kind == "provider_request":
        action.update(provider_request_id=str(owner["id"]), owner_execution_id=str(owner["execution_id"]))
    else:
        action["assignment_id"] = str(owner["id"])
    detail = ("/api/v3/assignments/" if kind == "assignment" else "/api/v3/reviewer-invocations/") + str(owner["id"])
    defect = (_completed_review_defect(conn, item, policy, descriptor, owner, kind)
              if owner["status"] == "completed" else None)
    if owner["status"] in {"failed", "interrupted", "cancelled"} or (defect and defect["retryable"]):
        if kind == "assignment":
            executions = tables["execution"]
            unsettled = conn.execute(select(executions.c.id).where(executions.c.assignment_id == owner["id"],
                executions.c.stop_confirmed_at.is_(None)).limit(1)).first()
            reason = "Confirm every previous reviewer execution stopped before retrying"
        else:
            claims = tables["resource_claim"]
            unsettled = conn.execute(select(claims.c.id).where(claims.c.provider_request_id == owner["id"],
                claims.c.released_at.is_(None)).limit(1)).first()
            reason = "Reconcile the previous review's unreleased provider reservations before retrying"
        if unsettled:
            action.update(action="reconcile_reviewer_owner", method="GET", endpoint=detail, required_fields=[], instructions=reason)
            return action, {"code": "review_unsettled", "owner_id": str(owner["id"]), "reason": reason}
        action.update(action="retry_reviewer_assignment" if standalone else "retry_reviewer_invocation",
            retry_of=str(owner["id"]), required_fields=["parent_request_id", "retry_note"], retry_note_required=True,
            retry_note_requirement="Explain the diagnosed failure and the concrete repair that makes this retry useful.")
        if defect:
            action["report_defect"] = defect
            action["instructions"] = "Repair the diagnosed report under the same reviewer, head, policy and review plan; preserve the earlier reports and independently assess all findings."
        return action, None
    action.update(method="GET", endpoint=detail, required_fields=[])
    if owner["status"] == "completed":
        action.update(action="repair_existing_reviewer_result",
            instructions=defect["reason"] if defect else
                "Preserve the completed substantive decision. Address its findings in the source; do not retry a valid approval or change request to obtain another verdict.")
        return action, {**(defect or {"code": "review_result_not_retryable"}), "owner_id": str(owner["id"])}
    action.update(action="collect_existing_reviewer_assignment" if kind == "assignment" else "collect_existing_reviewer_invocation",
        instructions="Use the owning maintainer to launch and attach its prepared child or collect its report and delivery; reconcile uncertain outcomes before retrying.")
    return action, None


def review_readiness(conn, item, policy=None, *, delivered_payload=None):
    """Explain authoritative current-head coverage without treating labels as evidence."""
    blockers = []
    if policy is None:
        policies, links = tables["review_policy"], tables["review_policy_repository"]
        candidates = list(conn.execute(select(policies).join(links).where(
            links.c.repository_id == item["repository_id"], policies.c.enabled.is_(True),
            policies.c.phases.contains([item.get("review_phase")]))).mappings())
        if len(candidates) == 1:
            policy = dict(candidates[0])
        else:
            blockers.append({"code": "review_policy_missing" if not candidates else "review_policy_ambiguous"})
    if policy:
        panel = postprocessing_review_panel(conn, item, policy, delivered_payload=delivered_payload)
        if not policy["enabled"] or item.get("review_phase") not in policy["phases"]:
            blockers.append({"code": "review_policy_inapplicable"})
    else:
        panel = {"required": [], "reviewed": [], "missing": []}
    required = [name for name in panel["required"] if name not in {"milestone-check", "accepted-route"}]
    missing = [name for name in panel["missing"] if name in required]
    for name in panel["missing"]:
        code = {"accepted-route": "missing_accepted_route", "milestone-check": "missing_milestone_check"}.get(name)
        if name.startswith("unresolved-review:"):
            code = "unresolved_review_finding"
        blockers.append({"code": code or ("missing_dimension" if name in required else "review_panel_incomplete"),
                         "requirement": name})
    for code in panel.get("invalid_carry_forward", []):
        blockers.append({"code": code})
    invalid = list(panel.get("invalid_reviews", []))
    if policy and "invalid_reviews" not in panel:
        rows, payloads = _review_history(conn, item)
        known = {entry["review_id"] for entry in invalid}
        for row in _latest_current_reviews(item, rows).values():
            payload = payloads.get(row["id"], {})
            if (row["verdict"] != "approved" and payload.get("rubric_verdict") != "approved") or str(row["id"]) in known:
                continue
            if not _current_approval(conn, row, payload, item, policy):
                descriptor = get(conn, "reviewer_descriptor", row["reviewer_descriptor_id"])
                invalid.append({"review_id": str(row["id"]), "reviewer_descriptor_id": str(descriptor["id"]),
                    "descriptor": descriptor["slug"], "code": "invalid_specialist_provenance",
                    "provider_request_id": str(row["provider_request_id"]) if row["provider_request_id"] else None,
                    "reason": "Independent review requires a completed, pinned invocation and current-head delivery"})
    actions = []
    review = tables["forge_review"]
    unresolved_descriptors = set(conn.execute(select(review.c.reviewer_descriptor_id).where(
        review.c.id.in_(panel.get("unresolved_review_ids", [])))).scalars())
    if policy and (missing or unresolved_descriptors):
        descriptors, links = tables["reviewer_descriptor"], tables["review_policy_reviewer"]
        for descriptor in conn.execute(select(descriptors).join(links).where(
                links.c.review_policy_id == policy["id"], descriptors.c.enabled.is_(True))
                .order_by(descriptors.c.slug)).mappings():
            dimensions = [name for name in missing if descriptor["slug"] == name or descriptor["slug"].endswith("-" + name)]
            if not dimensions and descriptor["id"] in unresolved_descriptors:
                dimensions = [name for name in required if descriptor["slug"] == name or descriptor["slug"].endswith("-" + name)]
            if dimensions:
                action, blocker = _review_next_action(conn, item, policy, descriptor, dimensions)
                if action["method"] == "POST":
                    name = "prepare_reviewer_assignment" if descriptor["invocation"] == "assignment" else "prepare_reviewer"
                    action["schema_lookup"] = {"section": "operations", "name": name}
                    action["schema_command"] = "horizon-pipeline agent schema --section operations --name " + name
                    action["request_template"] = {field: action[field] for field in (
                        "forge_item_id", "reviewer_descriptor_id", "expected_head_oid", "retry_of") if field in action}
                actions.append(action)
                if blocker:
                    blockers.append(blocker)
        configured = {name for action in actions for name in action["dimensions"]}
        for name in sorted(set(missing) - configured):
            actions.append({"action": "configure_reviewer_descriptor", "dimension": name})
    if not policy:
        actions.append({"action": "configure_review_policy", "repository_id": str(item["repository_id"]),
                        "phase": item.get("review_phase")})
    elif "<no enabled post-processing reviewer descriptors configured>" in panel["missing"]:
        actions.append({"action": "configure_review_panel", "policy_id": str(policy["id"])})
    if panel.get("unresolved_review_ids"):
        actions.append({"action": "resolve_review_findings", "review_ids": panel["unresolved_review_ids"]})
    if "accepted-route" in panel["missing"]:
        actions.append({"action": "accept_and_merge_route", "requirement": "accepted ancestor route covering these milestones"})
    if "milestone-check" in panel["missing"]:
        actions.append({"action": "verify_milestone_head", "expected_head_oid": item["head_commit_oid"]})
    return {"ready": not blockers, "forge_item_id": str(item["id"]), "head_commit_oid": item["head_commit_oid"],
        "target_branch": item["target_branch"], "policy_id": str(policy["id"]) if policy else None,
        "policy_revision": policy["revision"] if policy else None, "required_dimensions": required,
        "reviewed_dimensions": panel["reviewed"], "missing_dimensions": missing,
        "invalid_provenance": invalid, "blockers": blockers, "next_actions": actions, "panel": panel}


def _latest_current_reviews(item, rows):
    latest = {}
    for row in rows:
        if (row["commit_oid"] == item["head_commit_oid"] and row["reviewer_descriptor_id"]
                and "**Historical reviewer feedback:**" not in (row["summary"] or "")):
            latest.setdefault(row["reviewer_descriptor_id"], row)
    return latest


def _unresolved_reviews(conn, item, policy, rows, payloads, *, carried_review_ids=()):
    """An old-head objection survives a push until its reviewer confirms resolution."""
    objections = {str(row["id"]): row for row in rows if row["verdict"] == "changes_requested"
        and row["reviewer_descriptor_id"] is not None}
    if not objections:
        return []
    latest_ids = {row["id"] for row in _latest_current_reviews(item, rows).values()}
    resolved = set()
    for row in rows:
        if row["id"] not in latest_ids and str(row["id"]) not in carried_review_ids:
            continue
        payload = payloads.get(row["id"], {})
        if (not payload.get("reviewer_submission")
                or not _current_approval(conn, row, payload, item, policy,
                    carried=str(row["id"]) in carried_review_ids)):
            continue
        for resolution in payload.get("assessment", {}).get("resolutions", []):
            source = objections.get(resolution["review_id"])
            if (source and source["reviewer_descriptor_id"] == row["reviewer_descriptor_id"]
                    and source["observed_at"] <= row["observed_at"]):
                resolved.add(resolution["review_id"])
    return sorted(set(objections) - resolved)


def _with_findings(conn, panel, item, policy, rows, payloads, *, carried_review_ids=()):
    unresolved = _unresolved_reviews(conn, item, policy, rows, payloads, carried_review_ids=carried_review_ids)
    bases = {panel["base_commit_oid"]} if panel.get("base_commit_oid") else set()
    for row in _latest_current_reviews(item, rows).values():
        payload = payloads.get(row["id"], {})
        if (payload.get("review_plan") and payload.get("expected_base_oid")
                and _current_approval(conn, row, payload, item, policy)):
            bases.add(payload["expected_base_oid"])
    missing = list(panel["missing"])
    if len(bases) > 1 and "<reviewers inspected different target bases>" not in missing:
        missing.append("<reviewers inspected different target bases>")
    return {**panel, "unresolved_review_ids": unresolved,
        "base_commit_oid": next(iter(bases)) if len(bases) == 1 else None,
        "missing": [*missing, *[f"unresolved-review:{identifier}" for identifier in unresolved]]}


def _validate_resolutions(conn, data, item, *, reviewer_submission):
    resolutions = data.assessment.resolutions if data.assessment else []
    if resolutions and (not reviewer_submission or not data.provider_request_id or not data.reviewer_descriptor_id):
        raise DomainError("review_resolution_independence",
            "The independent reviewer must confirm its own findings. For a maintainer decision, omit "
            "assessment.resolutions and cite delivered specialist corrections in ordinary evidence instead. "
            "The acceptance gate still requires those independent resolutions.", 422)
    review = tables["forge_review"]
    prior_ids = list(conn.execute(select(review.c.id).where(review.c.forge_item_id == item["id"],
        review.c.reviewer_descriptor_id == data.reviewer_descriptor_id, review.c.verdict == "changes_requested")
        .order_by(review.c.observed_at, review.c.id)).scalars()) if resolutions or (
            reviewer_submission and data.verdict == "approved" and not data.historical
            and data.reviewer_descriptor_id) else []
    for resolution in resolutions:
        if resolution.review_id not in prior_ids:
            raise DomainError("review_resolution_invalid",
                "The resolution review_id does not identify a change request by this reviewer on this PR. "
                "Compare it with the immutable packet; copy the identifier exactly before resubmitting.", 422,
                invalid_review_id=str(resolution.review_id),
                review_ids=[str(identifier) for identifier in prior_ids[:20]],
                review_count=len(prior_ids), review_ids_truncated=len(prior_ids) > 20,
                review_url_template="/api/v3/records/forge_review/{id}")
    if reviewer_submission and data.verdict == "approved" and not data.historical and data.reviewer_descriptor_id:
        resolved = {resolution.review_id for resolution in resolutions}
        missing = [identifier for identifier in prior_ids if identifier not in resolved]
        if missing:
            raise DomainError("review_resolutions_required",
                "Approval must explicitly resolve this reviewer's earlier change requests in assessment.resolutions. "
                "Inspect each repair; use commented or changes_requested if findings remain unresolved.", 422,
                review_ids=[str(identifier) for identifier in missing],
                review_url_template="/api/v3/records/forge_review/{id}",
                resolution_fields=["review_id", "disposition (repaired or withdrawn)", "explanation", "evidence"])


def _carry_source(conn, item, policy, source_id):
    source = get(conn, "forge_review", source_id)
    if (source["forge_item_id"] != item["id"] or source["verdict"] != "approved"
            or not source["reviewer_descriptor_id"] or not source["provider_request_id"]
            or not source["reviewer_descriptor_revision_id"]
            or "**Historical reviewer feedback:**" in source["summary"]):
        raise DomainError("review_carry_invalid", "Reuse requires a same-PR, nonhistorical specialist approval", 409)
    descriptor = get(conn, "reviewer_descriptor", source["reviewer_descriptor_id"])
    revision = get(conn, "record_revision", source["reviewer_descriptor_revision_id"])
    link = tables["review_policy_reviewer"]
    if (not descriptor["enabled"] or revision["object_revision"] != descriptor["revision"]
            or str(revision["content"].get("id")) != str(descriptor["id"])
            or descriptor["project_id"] != policy["project_id"]
            or not conn.execute(select(link).where(link.c.review_policy_id == policy["id"],
                link.c.reviewer_descriptor_id == descriptor["id"])).first()):
        raise DomainError("review_carry_stale", "The specialist rubric or required panel changed; obtain a fresh review", 409)
    request = get(conn, "provider_request", source["provider_request_id"])
    if (request["status"] != "completed" or request["reason"] != "review"
            or request["reviewer_descriptor_id"] != descriptor["id"]
            or request["reviewer_descriptor_revision_id"] != revision["id"] or not request["guidance_manifest_artifact_id"]):
        raise DomainError("review_carry_invalid", "The original specialist invocation is not completed with pinned provenance", 409)
    reviews = tables["forge_review"]
    latest = conn.execute(select(reviews.c.id).where(reviews.c.forge_item_id == item["id"],
        reviews.c.reviewer_descriptor_id == descriptor["id"],
        ~reviews.c.summary.contains("**Historical reviewer feedback:**"))
        .order_by(reviews.c.observed_at.desc(), reviews.c.created_at.desc(), reviews.c.id.desc()).limit(1)).scalar_one()
    if latest != source["id"]:
        raise DomainError("review_carry_superseded", "A later specialist review supersedes this approval; reconcile its findings", 409)
    return source, descriptor, request


def _validate_carries(conn, item, policy, carries):
    if not policy["enabled"] or item["review_phase"] not in policy["phases"]:
        raise DomainError("review_carry_stale", "Review policy is no longer applicable", 409)
    descriptors = set()
    base = None
    for carry in carries:
        source, descriptor, _ = _carry_source(conn, item, policy, carry["source_review_id"])
        evidence = ReviewCarryForwardEvidence.model_validate(carry["evidence"])
        artifact = same_project(conn, "artifact", carry["evidence_artifact_id"], descriptor["project_id"])
        policy_revision = get(conn, "record_revision", carry["policy_revision_id"])
        guidance, document = tables["reviewer_guidance"], tables["document"]
        current_guidance = {str(row.id): row.revision for row in conn.execute(select(document.c.id, document.c.revision)
            .join(guidance, guidance.c.document_id == document.c.id)
            .where(guidance.c.reviewer_descriptor_id == descriptor["id"]))}
        if (artifact["kind"] != "blob" or artifact["content"].get("sha256") != carry["evidence_sha256"]
                or policy_revision["object_revision"] != policy["revision"]
                or str(policy_revision["content"].get("id")) != str(policy["id"])
                or str(source["reviewer_descriptor_revision_id"]) != carry["reviewer_descriptor_revision_id"]
                or str(descriptor["id"]) != carry["reviewer_descriptor_id"]
                or current_guidance != carry["guidance_revisions"]
                or evidence.forge_item_id != item["id"] or evidence.source_review_id != source["id"]
                or evidence.source_commit_oid != source["commit_oid"]
                or evidence.head_commit_oid != item["head_commit_oid"] or evidence.target_branch != item["target_branch"]):
            raise DomainError("review_carry_stale", "Carry-forward evidence no longer matches the PR, rubric or policy", 409)
        if descriptor["id"] in descriptors or (base and base != evidence.base_commit_oid):
            raise DomainError("review_carry_invalid", "Reuse dimensions must be distinct and inspect one target base", 422)
        descriptors.add(descriptor["id"])
        base = evidence.base_commit_oid
    return descriptors, base


def carried_review_payload(conn, item, policy):
    """Reuse is authoritative only after its maintainer assessment reaches Forge."""
    operation, reference, review, gate = (tables[name] for name in (
        "outbox_operation", "object_reference", "forge_review", "review_gate"))
    return conn.execute(select(operation.c.payload).join(reference, reference.c.id == operation.c.result_ref_id)
        .join(review, review.c.id == reference.c.forge_review_id)
        .join(gate, gate.c.maintainer_review_id == review.c.id).where(
            gate.c.policy_id == policy["id"], gate.c.status == "accepted",
            operation.c.kind == "forge_review", operation.c.status == "completed",
            operation.c.payload.has_key("carry_forward_evidence"),
            review.c.forge_item_id == item["id"], review.c.commit_oid == item["head_commit_oid"],
            review.c.reviewer_descriptor_id.is_(None), review.c.verdict == "approved",
            operation.c.payload["target_branch"].astext == item["target_branch"],
        ).order_by(review.c.observed_at.desc(), operation.c.created_at.desc()).limit(1)).scalar_one_or_none()


def postprocessing_review_panel(conn, item: dict, policy: dict, *, delivered_payload=None) -> dict:
    """Return current-head specialist coverage required by a library gate."""
    rows, payloads = _review_history(conn, item)
    from .milestones import review_panel
    milestones = review_panel(conn, item, policy, review_payloads=payloads)
    if milestones is not None:
        return _with_findings(conn, milestones, item, policy, rows, payloads)
    if item.get("review_phase") != "postprocessing":
        return _with_findings(conn, {"required": [], "reviewed": [], "missing": []}, item, policy, rows, payloads)
    links = tables["review_policy_reviewer"]
    descriptors = tables["reviewer_descriptor"]
    required = list(conn.execute(
        select(descriptors.c.id, descriptors.c.slug).join(
            links, links.c.reviewer_descriptor_id == descriptors.c.id
        ).where(
            links.c.review_policy_id == policy["id"],
            descriptors.c.enabled.is_(True),
        ).order_by(descriptors.c.slug)
    ).mappings())
    latest = _latest_current_reviews(item, rows)
    covered = {identifier for identifier, row in latest.items()
        if _current_approval(conn, row, payloads.get(row["id"]), item, policy)}
    bases = {payloads[row["id"]]["expected_base_oid"] for identifier, row in latest.items()
        if identifier in covered and payloads.get(row["id"], {}).get("expected_base_oid")}
    reviewed = [row["slug"] for row in required if row["id"] in covered]
    reused = []
    carried_review_ids = set()
    invalid = []
    payload = delivered_payload if delivered_payload is not None else carried_review_payload(conn, item, policy)
    if payload and payload.get("carry_forward_evidence"):
        try:
            carried, carry_base = _validate_carries(conn, item, policy, payload["carry_forward_evidence"])
        except (DomainError, ValidationError, KeyError) as error:
            invalid.append(getattr(error, "code", "review_carry_invalid"))
        else:
            if carry_base:
                bases.add(carry_base)
            carried_review_ids = {entry["source_review_id"] for entry in payload["carry_forward_evidence"]}
            reused = [row["slug"] for row in required if row["id"] in carried]
            covered.update(carried)
            reviewed = [row["slug"] for row in required if row["id"] in covered]
    missing = [row["slug"] for row in required if row["id"] not in covered]
    if not required:
        missing = ["<no enabled post-processing reviewer descriptors configured>"]
    if len(bases) > 1:
        missing.append("<reviewers inspected different target bases>")
    return _with_findings(conn, {
        "required": [row["slug"] for row in required],
        "reviewed": reviewed,
        "missing": missing,
        "carried_forward": reused,
        "invalid_carry_forward": invalid,
        "base_commit_oid": next(iter(bases)) if len(bases) == 1 else None,
    }, item, policy, rows, payloads, carried_review_ids=carried_review_ids)


def validate_postprocessing_report(summary: str, *, verdict: str, historical: bool) -> None:
    """Require inspectable report fields, without treating length as rigor."""
    if historical:
        return
    aliases = {
        "scope": "scope", "findings": "findings", "blocking findings": "findings",
        "verified": "evidence", "evidence": "evidence",
        "validation": "validation/limits", "limits": "validation/limits",
        "validation and limits": "validation/limits", "validation/limits": "validation/limits",
        "decision": "decision", "verdict": "decision",
    }
    labels = "|".join(re.escape(label) for label in sorted(aliases, key=len, reverse=True))
    headings = list(re.finditer(
        rf"^[ \t]*(?:[-#*]+[ \t]*)*(?:[🔍🔴✅🧪💬][ \t]*)?[*]*"
        rf"(?P<label>{labels})\b[*]*[ \t]*(?::[ \t]*)?", summary, re.IGNORECASE | re.MULTILINE))
    sections = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(summary)
        content = summary[heading.end():end].strip()
        if any(character.isalnum() for character in content):
            sections.setdefault(aliases[heading['label'].lower()], []).append(content)
    missing = [name for name in dict.fromkeys(aliases.values()) if name not in sections]
    if missing:
        raise DomainError(
            "review_report_incomplete",
            "Document scope, findings, evidence, validation or limits, and decision; concise reports are welcome",
            422,
            missing_sections=missing,
        )
    if verdict == "changes_requested" and all(re.fullmatch(
            r"(?:none(?: found)?|no (?:blocking )?(?:findings|issues|blockers)(?: found)?)[.!]?",
            content.strip(), re.IGNORECASE) for content in sections["findings"]):
        raise DomainError("review_report_incomplete", "Name the blocking change when requesting changes", 422,
                          missing_sections=["actionable findings"])


def maintainer_identity(conn, actor, policy, integration_id, requested=None):
    identifier = policy.get("maintainer_identity_id")
    if requested and requested != identifier:
        if not is_admin(conn, actor):
            raise DomainError("forbidden", "Use the policy's configured maintainer identity", 403)
        identifier = requested
    if identifier:
        identity = get(conn, "integration_identity", identifier)
        if not identity["enabled"] or identity["integration_id"] != integration_id:
            raise DomainError("maintainer_identity_unavailable", "Policy's Forge maintainer identity is not available")
        return str(identifier)
    return None


def queue_review(conn, actor, service, scheduler, raw: dict, key: str, *, reviewer_submission=False):
    data = ForgeReview.model_validate(raw)
    if data.provider_request_id and not data.reviewer_descriptor_id:
        raise DomainError("reviewer_required", "A reviewer invocation must identify its descriptor", 422)
    if data.reviewer_descriptor_revision_id and not data.reviewer_descriptor_id:
        raise DomainError("reviewer_required", "A descriptor revision requires its descriptor", 422)
    item = get(conn, "forge_item", data.forge_item_id)
    repository = get(conn, "repository", item["repository_id"])
    require_project(conn, actor, repository["project_id"], "worker" if reviewer_submission else "maintainer")
    if reviewer_submission:
        execution = live_execution(conn, actor)
        request = get(conn, "provider_request", data.provider_request_id)
        if request["execution_id"] != execution["id"] or request["reason"] != "review":
            raise DomainError("scope_mismatch", "Publish only the reviewer invocation in your own execution", 403)
    if item["kind"] != "pull_request" or (not data.historical and (
            item["status"] != "open" or item["head_commit_oid"] != data.commit_oid)):
        raise DomainError("review_head_changed", "Review must refer to the current open pull-request head")
    if not item["review_phase"]:
        raise DomainError("review_phase_unknown", "Classify the Forge item before choosing its review policy")
    if reviewer_submission and item["review_phase"] == "postprocessing" and not data.assessment:
        validate_postprocessing_report(data.summary, verdict=data.verdict, historical=data.historical)
    _validate_resolutions(conn, data, item, reviewer_submission=reviewer_submission)
    policy = scheduler.matching_policy(conn, repository["id"], item["review_phase"], required=True)
    payload = data.model_dump(mode="json", exclude_none=True)
    if reviewer_submission:
        payload["reviewer_submission"] = True
    payload["target_branch"] = item["target_branch"]
    payload["policy_revision_id"] = str(snapshot(conn, "review_policy", policy, actor.id))
    if data.reviewer_descriptor_id:
        descriptor = same_project(conn, "reviewer_descriptor", data.reviewer_descriptor_id, repository["project_id"])
        if not descriptor["enabled"]:
            raise DomainError("reviewer_unavailable", "Descriptor is disabled", 422)
        payload["reviewer_descriptor_revision_id"] = str(snapshot(conn, "reviewer_descriptor", descriptor, actor.id))
        if descriptor["integration_identity_id"]:
            identity = get(conn, "integration_identity", descriptor["integration_identity_id"])
            if not identity["enabled"] or identity["integration_id"] != repository["integration_id"]:
                raise DomainError("reviewer_identity_unavailable", "Descriptor's Forge identity is not available")
            payload["integration_identity_id"] = str(identity["id"])
        elif reviewer_submission:
            raise DomainError("reviewer_identity_unavailable", "Configure the reviewer's Forge account before publishing its assessment")
        elif data.integration_identity_id and not is_admin(conn, actor):
            raise DomainError("forbidden", "Use the descriptor's configured review identity", 403)
        elif not data.integration_identity_id:
            identity = maintainer_identity(conn, actor, policy, repository["integration_id"])
            if identity:
                payload["integration_identity_id"] = identity
        if data.provider_request_id:
            request = same_project(conn, "provider_request", data.provider_request_id, repository["project_id"])
            if request["reviewer_descriptor_id"] != descriptor["id"]:
                raise DomainError("reviewer_identity_mismatch", "Review result does not match its invocation", 422)
            if request["status"] != "completed" and not data.historical and not (
                    reviewer_submission and request["status"] in ("submitted", "running")):
                raise DomainError("review_unsettled", "Use historical commented feedback to preserve findings without claiming a completed approval")
            if data.historical and request["status"] == "pending":
                raise DomainError("review_unsettled", "An unstarted reservation has no reviewer findings to publish")
            if not request["guidance_manifest_artifact_id"] or not request["reviewer_descriptor_revision_id"]:
                raise DomainError("review_manifest_missing", "Review invocation lacks pinned provenance")
            artifact = get(conn, "artifact", request["guidance_manifest_artifact_id"])
            manifest = json.loads(service.store.read(artifact["content"]["sha256"]))
            if manifest.get("forge_item_id") != str(item["id"]):
                raise DomainError("review_target_mismatch", "Reviewer inspected a different Forge item", 422)
            if manifest.get("head_commit_oid") != data.commit_oid:
                raise DomainError("review_head_changed", "Reviewer inspected a different commit")
            if not data.historical and ("target_branch" not in manifest or manifest["target_branch"] != item["target_branch"]):
                raise DomainError("review_base_changed", "Reviewer must inspect the current target branch's diff")
            if manifest.get("review_plan") and not data.historical:
                plan = manifest["review_plan"]
                if data.verdict == "approved":
                    selected = {question["dimension"] for question in plan["questions"]}
                    if (not data.assessment or not data.assessment.classification_confirmed
                            or not selected.issubset(data.assessment.dimensions)):
                        raise DomainError("review_plan_incomplete", "Confirm the scope classification and assess every selected question", 422)
                payload["review_plan"] = plan
                payload["expected_base_oid"] = plan["base_commit_oid"]
            payload["reviewer_descriptor_revision_id"] = str(request["reviewer_descriptor_revision_id"])
            payload["policy_revision_id"] = manifest["policy_revision_id"]
            # Historical feedback uses the descriptor's current publishing account;
            # its original invocation/rubric remains pinned in the record.
            if manifest.get("integration_identity_id") and not data.historical and not reviewer_submission:
                identity = get(conn, "integration_identity", manifest["integration_identity_id"])
                if not identity["enabled"] or identity["integration_id"] != repository["integration_id"]:
                    raise DomainError("reviewer_identity_unavailable", "Pinned reviewer identity is not available")
                payload["integration_identity_id"] = str(identity["id"])
            if data.historical:
                payload["summary"] = ("**Historical reviewer feedback:** This preserves findings on the inspected revision; "
                    "it does not approve the current PR. "
                    f"Recorded invocation status: `{request['status']}`.\n\n" + data.summary)
        revision = get(conn, "record_revision", payload["reviewer_descriptor_revision_id"])
        rubric = revision["content"]
        title = rubric["slug"].replace("_", " ").replace("-", " ").title()
        invocation = "Reviewer result" if data.provider_request_id else "Maintainer applying this rubric"
        if data.verdict == "approved" and not data.provider_request_id:
            payload["rubric_verdict"] = "approved"
            payload["verdict"] = "commented"
            invocation += "; this assessment does not establish independent specialist coverage"
        payload["summary"] = (f"### Horizon Review: {title}\n\n"
            f"Reviewer: `{rubric['slug']}` | Rubric revision: {revision['object_revision']} | "
            f"Commit: `{data.commit_oid[:12]}`\n\n{invocation}.\n\n{payload['summary']}")
    else:
        identity = maintainer_identity(conn, actor, policy, repository["integration_id"], data.integration_identity_id)
        if identity:
            payload["integration_identity_id"] = identity
    if data.carry_forward:
        if item["review_phase"] != "postprocessing":
            raise DomainError("review_carry_invalid", "Evidence reuse applies to post-processing review panels", 422)
        carries = []
        attribution = []
        for entry in data.carry_forward:
            source, descriptor, request = _carry_source(conn, item, policy, entry.source_review_id)
            artifact = same_project(conn, "artifact", entry.evidence_artifact_id, repository["project_id"])
            manifest_artifact = same_project(conn, "artifact", request["guidance_manifest_artifact_id"], repository["project_id"])
            try:
                if artifact["kind"] != "blob" or artifact["content"]["size_bytes"] > 65536:
                    raise ValueError("Expected a bounded JSON evidence blob")
                evidence = ReviewCarryForwardEvidence.model_validate_json(service.store.read(
                    artifact["content"]["sha256"], artifact["content"]["size_bytes"]))
                manifest = json.loads(service.store.read(manifest_artifact["content"]["sha256"],
                                                        manifest_artifact["content"]["size_bytes"]))
            except (ValueError, KeyError) as error:
                raise DomainError("review_carry_invalid", "Provide a valid immutable carry-forward evidence blob", 422) from error
            if (manifest.get("forge_item_id") != str(item["id"])
                    or manifest.get("head_commit_oid") != source["commit_oid"]
                    or manifest.get("review_phase") != item["review_phase"]
                    or manifest.get("target_branch") != item["target_branch"]):
                raise DomainError("review_carry_stale", "Original reviewer provenance differs from the requested PR or base branch", 409)
            carries.append({"source_review_id": str(source["id"]), "evidence_artifact_id": str(artifact["id"]),
                "evidence_sha256": artifact["content"]["sha256"], "evidence": evidence.model_dump(mode="json"),
                "reviewer_descriptor_id": str(descriptor["id"]),
                "reviewer_descriptor_revision_id": str(source["reviewer_descriptor_revision_id"]),
                "guidance_revisions": {value["id"]: value["revision"] for value in manifest["guidance"]},
                "policy_revision_id": manifest.get("policy_revision_id")})
            attribution.append(f"- `{descriptor['slug']}`: original Forge review `{source['remote_id']}` "
                f"at `{source['commit_oid'][:12]}`; [carry-forward evidence]"
                f"({service.config.public_url.rstrip('/')}/api/v3/artifacts/{artifact['id']}/content). "
                f"{evidence.rationale}")
        _, base = _validate_carries(conn, item, policy, carries)
        payload["carry_forward_evidence"] = carries
        payload["expected_base_oid"] = base
        payload["summary"] += ("\n\n### Maintainer evidence carry-forward\n\n"
            "These are maintainer attestations of unchanged review scope, not fresh specialist approvals. "
            f"Inspected target base: `{base}`.\n\n" + "\n".join(attribution))
    if data.verdict == "approved" and not data.reviewer_descriptor_id and not data.historical:
        readiness = review_readiness(conn, item, policy, delivered_payload=payload)
        if not readiness["ready"]:
            raise DomainError("review_gate_blocked", "Final approval requires the current-head checks and independent specialist coverage", 409,
                              review_readiness=readiness)
    return create(conn, "outbox_operation", project_id=repository["project_id"], actor_principal_id=actor.id,
                  kind="forge_review", schema_version=1, idempotency_key=key, payload=payload)


def queue_merge(conn, actor, raw: dict, key: str):
    data = ForgeMerge.model_validate(raw)
    item = get(conn, "forge_item", data.forge_item_id)
    project_id = project_of(conn, "forge_item", item["id"])
    require_project(conn, actor, project_id, "maintainer")
    gate = same_project(conn, "review_gate", data.review_gate_id, project_id)
    if (gate["forge_item_id"] != item["id"] or gate["status"] != "accepted"
            or item["head_commit_oid"] != data.expected_head_oid or gate["accepted_commit_oid"] != data.expected_head_oid):
        raise DomainError("review_gate_stale", "An accepted review of this exact head is required")
    policy = get(conn, "review_policy", gate["policy_id"])
    panel = postprocessing_review_panel(conn, item, policy)
    if panel["missing"] or panel.get("invalid_carry_forward"):
        raise DomainError(
            "review_panel_incomplete",
            ("Post-processing merge" if item.get("review_phase") == "postprocessing" else "Milestone merge")
            + " requires the current-head checks and specialist coverage required by the repository workflow",
            409,
            required_reviewers=panel["required"],
            reviewed_reviewers=panel["reviewed"],
            missing_reviewers=panel["missing"],
        )
    payload = data.model_dump(mode="json", exclude_none=True)
    payload["expected_base"] = item["target_branch"]
    if panel.get('base_commit_oid'):
        payload['expected_base_oid'] = panel['base_commit_oid']
    carried = carried_review_payload(conn, item, policy)
    if carried:
        _, payload["expected_base_oid"] = _validate_carries(conn, item, policy, carried["carry_forward_evidence"])
    repository = get(conn, "repository", item["repository_id"])
    identity = maintainer_identity(conn, actor, policy, repository["integration_id"], data.integration_identity_id)
    if identity:
        payload["integration_identity_id"] = identity
    return create(conn, "outbox_operation", project_id=project_id, actor_principal_id=actor.id,
                  kind="forge_merge", schema_version=1, idempotency_key=key,
                  payload=payload)


def queue_label(conn, actor, raw: dict, key: str):
    data = ForgeLabel.model_validate(raw)
    project_id = project_of(conn, "forge_item", data.forge_item_id)
    require_project(conn, actor, project_id, "worker")
    return create(conn, "outbox_operation", project_id=project_id, actor_principal_id=actor.id,
                  kind="forge_label", schema_version=1, idempotency_key=key, payload=data.model_dump(mode="json"))


def queue_comment(conn, actor, data, scheduler, key):
    item = get(conn, "forge_item", data.forge_item_id)
    repository = get(conn, "repository", item["repository_id"])
    require_project(conn, actor, repository["project_id"], "worker")
    payload = data.model_dump(mode="json")
    if actor.kind == "agent" and live_execution(conn, actor)["role"] == "maintainer" and item["review_phase"]:
        policy = scheduler.matching_policy(conn, repository["id"], item["review_phase"])
        if policy:
            identity = maintainer_identity(conn, actor, policy, repository["integration_id"])
            if identity:
                payload["integration_identity_id"] = identity
    return create(conn, "outbox_operation", project_id=repository["project_id"], actor_principal_id=actor.id,
        kind="forge_comment", schema_version=1, idempotency_key=key, payload=payload)


def queue_edit(conn, actor, data, key):
    item = get(conn, "forge_item", data.forge_item_id)
    project_id = project_of(conn, "forge_item", item["id"])
    require_project(conn, actor, project_id, "maintainer")
    if (item["kind"] != "pull_request" or item["head_commit_oid"] != data.expected_head_oid
            or item["target_branch"] != data.expected_base or item["status"] == "merged"):
        raise DomainError("review_head_changed", "Inspect the current unmerged PR before editing its base or state", 409)
    return create(conn, "outbox_operation", project_id=project_id, actor_principal_id=actor.id,
        kind="forge_edit", schema_version=1, idempotency_key=key, payload=data.model_dump(mode="json", exclude_none=True))
