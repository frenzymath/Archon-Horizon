"""Bounded review briefings backed by a complete immutable context artifact."""

from __future__ import annotations

import json
from urllib.parse import urlencode

from sqlalchemy import select

from .records import json_value, save_blob
from .schema import tables

PACKET_BYTES = 16 * 1024


def encode(packet):
    # Bound the ASCII representation too: CLI JSON output escapes Unicode.
    return json.dumps(packet, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def prepare_packet(conn, service, *, project_id, execution_id, item, repository,
                   descriptor, descriptor_revision, policy, policy_revision, functions,
                   guidance, instructions, report_schema, milestone_evidence,
                   milestone_discovery, report_request_id, review_plan=None):
    review = tables["forge_review"]
    prior_objections = [{**dict(row), "detail_url": f"/api/v3/records/forge_review/{row['id']}"}
        for row in conn.execute(select(review.c.id, review.c.commit_oid, review.c.summary).where(
            review.c.forge_item_id == item["id"], review.c.reviewer_descriptor_id == descriptor["id"],
            review.c.verdict == "changes_requested").order_by(review.c.observed_at, review.c.id)).mappings()]
    pins = json_value({"project_id": project_id, "repository_id": repository["id"],
        "forge_item_id": item["id"], "forge_item_revision": item["revision"],
        "head_commit_oid": item["head_commit_oid"], "target_branch": item["target_branch"],
        "policy_id": policy["id"], "policy_revision_id": policy_revision,
        "reviewer_descriptor_id": descriptor["id"],
        "reviewer_descriptor_revision_id": descriptor_revision})
    context = json_value({"schema_version": 1, "pins": pins,
        "policy": {"instructions": policy["instructions"], "revision": policy["revision"]},
        "descriptor": {"slug": descriptor["slug"], "revision": descriptor["revision"],
                       "instructions": descriptor["instructions"]},
        "function_instructions": functions, "review_instructions": instructions,
        "guidance": guidance, "report_schema": report_schema,
        "milestone_evidence": milestone_evidence, "milestone_discovery": milestone_discovery,
        "review_plan": review_plan, "prior_objections": prior_objections})
    artifact = save_blob(conn, service.store, project_id, context, execution_id)
    content_url = f"/api/v3/artifacts/{artifact['id']}/content"
    inspect_url = f"/api/v3/forge-items/{item['id']}/inspect?"
    links = {view: inspect_url + urlencode({"view": view,
        "expected_head_oid": item["head_commit_oid"], "limit": 100 if view == "diff" else 20})
        for view in ("diff", "files", "reviews", "comments", "review_comments")}
    links["review_comments"] += "&review_id={remote_review_number}"
    links["target_head"] = f"/api/v3/repositories/{repository['id']}/head?" + urlencode(
        {"branch": item["target_branch"]})
    links["source_file"] = f"/api/v3/repositories/{repository['id']}/file?" + urlencode(
        {"commit_oid": item["head_commit_oid"]}) + "&path={urlencoded_path}"
    packet = {"schema_version": 1, "pins": pins,
        "complete_context": {"url": content_url, "sha256": artifact["content"]["sha256"]},
        "evidence_links": links,
        "response_fields": {
            "inspect": "items; diff; next_page; review_readiness.panel.check_id. "
                       "For diff, limit counts lines; other views paginate items.",
            "milestone_check": "id, kind, source_commit_oid, base_commit_oid; report contains compiled, "
                               "toolchain, manifest_digest, targets, types, definitions and direct_admissions",
            "repository_file": "text (decoded UTF-8, or null); content_base64; commit_oid; path"},
        "report": {"method": "POST", "url": f"/api/v3/reviewer-invocations/{report_request_id}/report",
            "minimal_body": {"verdict": "commented", "summary": "Your assessment and evidence", "historical": False},
            "minimal_body_scope": "Commented feedback only; approval must satisfy the requirements below.",
            "verdicts": report_schema["properties"]["verdict"]["enum"],
            "typed_schema": {"context_json_pointer": "/report_schema"},
            "assessment_required_for_approval": bool(review_plan or prior_objections),
            "delivery_url": "/api/v3/operations/{response.idempotency_key}?operation=reviewer_report",
            "delivery": "Use the report response's idempotency_key, not its id (outbox UUID). "
                        "The receipt namespace is reviewer_report even though response.kind is forge_review. "
                        "status=completed and result_ref_id establish delivery, not semantic acceptance."},
        "snapshot_limits": "Preparation evidence only. Independently assess the source and rubric. "
            "Refresh relevant discussion and head/base pins before reporting; changed pins require reassessment "
            "or historical commented feedback. Omitted instructions remain required and are in complete_context."}
    if review_plan:
        packet["report"]["assessment_template"] = {
            "scope": "Replace with the inspected scope and pins",
            "dimensions": [question["dimension"] for question in review_plan["questions"]],
            "complete": False, "classification_confirmed": False,
            "evidence": ["Replace with concrete review evidence"],
            "limitations": [], "findings": [], "resolutions": []}
        packet["report"]["approval_requirements"] = (
            "Replace template text with your assessment. Independently confirm the scope/risk classification "
            "and assess every selected dimension before setting classification_confirmed and complete true. "
            "Approval requires no unresolved blocking findings. Resolve your own prior objections explicitly "
            "in resolutions; an empty list does not withdraw them.")
    if prior_objections:
        packet["report"]["prior_objection_requirements"] = (
            "Before approval, inspect every prior_objections entry, including omitted entries in complete_context. "
            "Explicitly confirm each earlier change request in the report's assessment.resolutions with its "
            "review_id, repaired or withdrawn disposition, explanation and evidence. This remains your independent "
            "semantic decision; use commented or changes_requested when findings remain unresolved. "
            "If no assessment_template is supplied, use typed_schema and choose the dimensions you assessed.")
        if not review_plan:
            packet["report"]["approval_requirements"] = packet["report"]["prior_objection_requirements"]
        prior = {"count": len(prior_objections), "items": [], "truncated": True,
                 "context_json_pointer": "/prior_objections"}
        for row in context["prior_objections"][:8]:
            prior["items"].append({"id": row["id"], "commit_oid": row["commit_oid"],
                "summary_excerpt": row["summary"][:200], "summary_truncated": len(row["summary"]) > 200,
                "detail_url": row["detail_url"]})
            if len(encode(prior)) > 3200:
                prior["items"].pop()
                break
        prior["truncated"] = len(prior["items"]) < len(prior_objections)
        packet["prior_objections"] = prior
    # Keep the selected rubric intact whenever it fits. Large custom sections
    # remain exact in the artifact and are explicitly linked, never silently cut.
    sections = (("descriptor", 6000), ("policy", 4000), ("review_instructions", 2000),
                ("function_instructions", 1400), ("milestone_evidence", 2500),
                ("milestone_discovery", 1600), ("guidance", 1500), ("review_plan", 1600))
    for name, maximum in sections:
        value = context[name]
        placeholder = {"omitted": True, "context_json_pointer": "/" + name,
                       "encoded_bytes": len(encode(value))}
        packet[name] = value if len(encode(value)) <= maximum else placeholder
        if len(encode(packet)) > PACKET_BYTES - 1800:
            packet[name] = placeholder
    if len(encode(packet)) > PACKET_BYTES:
        # Unusually long route/branch identifiers still cannot create an
        # unbounded prompt. The complete immutable context preserves every pin.
        packet = {"schema_version": 1, "complete_context": packet["complete_context"],
            "omitted": True, "context_json_pointer": "",
            "reason": "Review context identifiers exceed the inline packet budget; read complete_context."}
    return packet, artifact
