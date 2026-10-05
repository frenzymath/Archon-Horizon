"""Durable, per-perspective review state on the Forge PR."""

import json
from sqlalchemy import select

from .errors import DomainError
from .records import create, get
from .schema import tables

STATES = ("queued", "running", "ok", "requesting-changes", "commented", "historical", "cancelled")
TERMINAL_STATES = ("merged", "closed", "superseded")


def terminal_state(item):
    """Return the stable review outcome label for a terminal Forge item.

    Forgejo exposes only ``merged`` versus ``closed``.  Projects may preserve
    the more useful superseded distinction through an explicit label on the
    item (for example ``superseded`` or ``incorporated``); otherwise ``closed``
    is deliberately used instead of guessing from the PR body.
    """
    status = item.get("status")
    if status == "merged":
        return "merged"
    if status != "closed":
        return None
    labels = {str(value).casefold() for value in item.get("labels", [])}
    if labels.intersection({"superseded", "incorporated", "obsolete", "duplicate",
                            "review/superseded", "review/incorporated"}):
        return "superseded"
    return "closed"


def terminal_label_operation(conn, service, actor_id, item, *, key=None):
    """Queue one idempotent terminal-label reconciliation for a Forge item."""
    state = terminal_state(item)
    if state is None:
        return None
    terminal = "review/" + state
    removable = {"awaiting-review"}
    removable.update("review/" + value for value in TERMINAL_STATES)
    removable.update(label for label in item.get("labels", []) if (
        label.startswith("review/") and label.count("/") == 2
        and label.rsplit("/", 1)[1] in STATES))
    remove = sorted(removable.intersection(item.get("labels", [])))
    repository = get(conn, "repository", item["repository_id"])
    policies, repositories = tables["review_policy"], tables["review_policy_repository"]
    policy = conn.execute(select(policies).join(repositories,
        repositories.c.review_policy_id == policies.c.id).where(
        repositories.c.repository_id == repository["id"], policies.c.enabled.is_(True),
        policies.c.phases.contains([item["review_phase"]]) if item.get("review_phase") else True
    )).mappings().first()
    if policy:
        removable.update(policy["attention_labels"] or [])
        remove = sorted(removable.intersection(item.get("labels", [])))
    if terminal in item.get("labels", []) and not remove:
        return None
    payload = {"forge_item_id": str(item["id"]), "add": [terminal], "remove": remove}
    if policy and policy["maintainer_identity_id"]:
        identity = get(conn, "integration_identity", policy["maintainer_identity_id"])
        if identity["enabled"] and identity["integration_id"] == repository["integration_id"]:
            payload["integration_identity_id"] = str(identity["id"])
    operation_key = key or f"terminal-review-label:{item['id']}:{state}"
    existing = conn.execute(select(tables["outbox_operation"]).where(
        tables["outbox_operation"].c.actor_principal_id == actor_id,
        tables["outbox_operation"].c.kind == "forge_label",
        tables["outbox_operation"].c.idempotency_key == operation_key)).mappings().first()
    if existing:
        return dict(existing)
    return create(conn, "outbox_operation", project_id=repository["project_id"],
                  actor_principal_id=actor_id, kind="forge_label", schema_version=1,
                  idempotency_key=operation_key, payload=payload)


def queue_state(conn, service, actor_id, item, descriptor, state, key, request=None):
    if state not in STATES:
        raise ValueError("Unknown review label state")
    # A delayed result from an older round must not overwrite a newer round.
    if request:
        table = tables["provider_request"]
        newer = conn.execute(select(table.c.guidance_manifest_artifact_id).where(
            table.c.reviewer_descriptor_id == descriptor["id"], table.c.created_at > request["created_at"],
            table.c.guidance_manifest_artifact_id.is_not(None))).scalars()
        for identifier in newer:
            artifact = get(conn, "artifact", identifier)
            manifest = json.loads(service.store.read(artifact["content"]["sha256"]))
            if manifest.get("forge_item_id") == str(item["id"]):
                return None
        assignment, link = tables["assignment"], tables["assignment_artifact"]
        queued = conn.execute(select(link.c.artifact_id).join(assignment, assignment.c.id == link.c.assignment_id).where(
            assignment.c.reviewer_descriptor_id == descriptor["id"], assignment.c.created_at > request["created_at"],
            assignment.c.status.in_(("pending", "running")), link.c.purpose == "reviewer_manifest")).scalars()
        for identifier in queued:
            artifact = get(conn, "artifact", identifier)
            manifest = json.loads(service.store.read(artifact["content"]["sha256"]))
            if manifest.get("forge_item_id") == str(item["id"]):
                return None
    prefix = "review/" + descriptor["slug"] + "/"
    table = tables["outbox_operation"]
    key = "review-label:" + key
    existing = conn.execute(select(table).where(table.c.actor_principal_id == actor_id,
        table.c.kind == "forge_label", table.c.idempotency_key == key)).mappings().first()
    if existing:
        return dict(existing)
    repository = get(conn, "repository", item["repository_id"])
    payload = {"forge_item_id": str(item["id"]), "add": [prefix + state],
               "remove": [prefix + other for other in STATES if other != state]}
    policies, repositories = tables["review_policy"], tables["review_policy_repository"]
    policy = conn.execute(select(policies).join(repositories,
        repositories.c.review_policy_id == policies.c.id).where(
        repositories.c.repository_id == repository["id"], policies.c.enabled.is_(True),
        policies.c.phases.contains([item["review_phase"]]))).mappings().one_or_none()
    # Lifecycle labels are control-plane projections. Specialist accounts keep
    # read-only repository access; their assessments use their own identity.
    identity_id = policy["maintainer_identity_id"] if policy else None
    if identity_id:
        identity = get(conn, "integration_identity", identity_id)
        if not identity["enabled"] or identity["integration_id"] != repository["integration_id"]:
            raise DomainError("maintainer_identity_unavailable", "Policy's Forge maintainer identity is not available")
        payload["integration_identity_id"] = str(identity["id"])
    return create(conn, "outbox_operation", project_id=repository["project_id"], actor_principal_id=actor_id,
        kind="forge_label", schema_version=1, idempotency_key=key, payload=payload)


def invocation_state(conn, service, actor_id, request, state, key):
    if state == "cancelled":
        operations = tables["outbox_operation"]
        # Cleanup of a provider reservation does not retract its published review.
        if conn.execute(select(operations.c.id).where(operations.c.kind == "forge_review",
                operations.c.payload["provider_request_id"].astext == str(request["id"]),
                operations.c.status.in_(("pending", "running", "uncertain", "completed"))).limit(1)).first():
            return None
    artifact = get(conn, "artifact", request["guidance_manifest_artifact_id"])
    manifest = json.loads(service.store.read(artifact["content"]["sha256"]))
    return queue_state(conn, service, actor_id, get(conn, "forge_item", manifest["forge_item_id"]),
        get(conn, "reviewer_descriptor", request["reviewer_descriptor_id"]), state, key, request)
