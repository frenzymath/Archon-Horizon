"""Scoped proposals for atomic file changes on isolated Forge branches."""

from __future__ import annotations

from ..auth import live_execution, require_project
from ..errors import DomainError
from ..models import ForgeChange
from ..persistence.records import create, get, same_project


def queue(conn, actor, data: ForgeChange, key: str):
    repository = get(conn, "repository", data.repository_id)
    project_id = repository["project_id"]
    require_project(conn, actor, project_id, "worker")
    run = same_project(conn, "run", data.origin_run_id, project_id)
    if data.forge_item_id:
        require_project(conn, actor, project_id, "maintainer")
        item = same_project(conn, "forge_item", data.forge_item_id, project_id)
        if (item["repository_id"] != repository["id"] or item["kind"] != "pull_request"
                or item["status"] != "open" or item["head_commit_oid"] != data.base_commit_oid):
            raise DomainError("review_head_changed", "Amend only the current head of an open PR in this repository", 409)
    if actor.kind == "agent" and live_execution(conn, actor)["run_id"] != run["id"]:
        raise DomainError("scope_mismatch", "Forge origin must be the submitting agent's run", 422)
    integration = get(conn, "integration", repository["integration_id"])
    if integration["kind"] != "forge" or not integration["enabled"]:
        raise DomainError("forge_unavailable", "The repository requires an enabled Forge integration", 422)
    total = 0
    for file in data.files:
        if file.content_artifact_id is None:
            continue
        artifact = same_project(conn, "artifact", file.content_artifact_id, project_id)
        if artifact["kind"] != "blob":
            raise DomainError("invalid_file_artifact", "File content must be an uploaded blob", 422)
        size = artifact["content"]["size_bytes"]
        if size > 1024**2:
            raise DomainError("file_too_large", "Each proposed file is limited to 1 MiB", 422)
        total += size
    if total > 8 * 1024**2:
        raise DomainError("change_too_large", "One proposed commit is limited to 8 MiB", 422)
    operation = create(conn, "outbox_operation", project_id=project_id, actor_principal_id=actor.id,
        kind="forge_change", schema_version=1, idempotency_key=key, payload=data.model_dump(mode="json"))
    return {"operation": operation, "branch": None if data.forge_item_id else f"horizon/changes/{operation['id']}"}
