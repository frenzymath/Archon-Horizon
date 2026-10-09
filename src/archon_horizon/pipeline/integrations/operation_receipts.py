"""Current delivery state alongside immutable submission receipts."""

from uuid import UUID

from sqlalchemy import select

from ..auth import require_project
from ..errors import DomainError
from ..persistence.records import get
from ..persistence.schema import tables


DELIVERIES = {
    "forge_change": "forge_change", "forge_create": "forge_create",
    "forge_review": "forge_review", "reviewer_report": "forge_review",
    "forge_comment": "forge_comment", "forge_edit": "forge_edit",
    "forge_merge": "forge_merge", "forge_label": "forge_label",
    "discussion_reply": "zulip_post",
}


def current_receipt(conn, actor, receipt, original):
    kind = DELIVERIES.get(receipt["operation"])
    if kind is None or not isinstance(original, dict):
        return original
    wrapped = isinstance(original.get("operation"), dict)
    submitted = original["operation"] if wrapped else original
    if submitted.get("kind") != kind or not submitted.get("id"):
        return original
    operation = get(conn, "outbox_operation", UUID(str(submitted["id"])))
    if operation["project_id"] != receipt["project_id"] or operation["kind"] != kind:
        raise DomainError("receipt_scope_mismatch", "Delivery does not match its submission receipt", 409)
    require_project(conn, actor, operation["project_id"])
    result = {**original, "operation": operation} if wrapped else dict(operation)
    if kind == "forge_change" and operation["status"] == "completed" and operation["result_ref_id"]:
        result["publication_receipt"] = publication_receipt(conn, operation)
        if wrapped and result["publication_receipt"]["branch"]:
            result["branch"] = result["publication_receipt"]["branch"]
    return result


def publication_receipt(conn, operation):
    reference = get(conn, "object_reference", operation["result_ref_id"])
    if reference["kind"] != "artifact":
        raise DomainError("invalid_publication_receipt", "Delivery result is not a commit artifact", 409)
    artifact = get(conn, "artifact", reference["artifact_id"])
    payload = operation["payload"]
    if (artifact["kind"] != "commit" or artifact["project_id"] != operation["project_id"]
            or str(artifact["content"].get("repository_id")) != payload["repository_id"]):
        raise DomainError("invalid_publication_receipt", "Commit artifact does not match this delivery", 409)
    table = tables["publication"]
    branch = None if payload.get("forge_item_id") else "horizon/changes/" + str(operation["id"])
    # A commit can be published on multiple refs. Amendments retain the PR id;
    # without a recorded branch binding, never guess from another publication.
    publication = conn.execute(select(table).where(table.c.artifact_id == artifact["id"],
        table.c.status == "verified", table.c.target["repository_id"].astext == payload["repository_id"],
        table.c.target["ref_name"].astext == "refs/heads/" + branch)
        .order_by(table.c.created_at.desc(), table.c.id).limit(1)).mappings().first() if branch else None
    return {
        "operation_id": operation["id"], "artifact_id": artifact["id"],
        "publication_id": publication["id"] if publication else None,
        "forge_item_id": payload.get("forge_item_id"),
        "repository_id": payload["repository_id"], "branch": branch,
        "base_commit_oid": payload["base_commit_oid"], "commit_oid": artifact["content"]["commit_oid"],
        "verified_at": publication["verified_at"] if publication else operation["updated_at"],
        "verification": "git_tree_delta",
        "scope": "Exact requested file operations, commit parent, and preservation of unchanged tree entries",
        "changed_paths": [file["path"] for file in payload["files"]],
        "lean_validation_included": False,
    }
