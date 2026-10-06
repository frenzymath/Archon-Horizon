"""Scoped, bounded Forge reads without lending integration credentials to agents."""

from __future__ import annotations

import re
from urllib.parse import quote

from .auth import require_project
from .connectors import ConnectorFailure, ForgejoClient, SecretResolver
from .errors import DomainError
from .records import get


def repository_head(database, config, authenticate, repository_id, branch=None):
    with database.transaction() as conn:
        actor = authenticate(conn)
        repository = get(conn, "repository", repository_id)
        require_project(conn, actor, repository["project_id"])
        integration = get(conn, "integration", repository["integration_id"])
        if integration["kind"] != "forge" or not integration["enabled"] or not repository["remote_path"]:
            raise DomainError("forge_unavailable", "Repository has no enabled Forge integration", 503)
    remote = None
    branch_name = branch or repository["default_branch"]
    if (not isinstance(branch_name, str) or len(branch_name) > 1024
            or any(ord(character) < 32 for character in branch_name)):
        raise DomainError("invalid_branch", "A published branch name is required", 422)
    try:
        secret = SecretResolver(config.state_root)(integration["credential_ref"])
        remote = ForgejoClient(integration["endpoint"], secret.get("token", ""))
        resolved = remote.request("GET", remote.repository_path(repository["remote_path"]) +
                                  "/branches/" + quote(branch_name, safe=""))
        branch_commit = resolved.get("commit") if isinstance(resolved, dict) else None
        commit = branch_commit.get("id") if isinstance(branch_commit, dict) else None
        if not isinstance(commit, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
            raise ConnectorFailure("invalid_branch_head")
    except ConnectorFailure as error:
        raise DomainError(error.code, "The published repository head could not be read", 503 if error.transient else 422) from None
    finally:
        if remote is not None:
            remote.close()
    with database.transaction() as conn:
        require_project(conn, authenticate(conn), repository["project_id"])
    return {"repository_id": repository_id, "branch": branch_name, "commit_oid": commit}


def inspect(database, config, authenticate, *, repository_id=None, forge_item_id=None,
            file_path=None, commit_oid=None, view=None, expected_head_oid=None, page=1, limit=50, review_id=None):
    with database.transaction() as conn:
        actor = authenticate(conn)
        item = get(conn, "forge_item", forge_item_id) if forge_item_id else None
        repository = get(conn, "repository", item["repository_id"] if item else repository_id)
        require_project(conn, actor, repository["project_id"])
        integration = get(conn, "integration", repository["integration_id"])
        if integration["kind"] != "forge" or not integration["enabled"] or not repository["remote_path"]:
            raise DomainError("forge_unavailable", "Repository has no enabled Forge integration", 503)
    remote = None
    try:
        secret = SecretResolver(config.state_root)(integration["credential_ref"])
        remote = ForgejoClient(integration["endpoint"], secret.get("token", ""))
        if item:
            result = remote.inspect_item(repository["remote_path"], item["remote_number"], kind=item["kind"],
                view=view, expected_head_oid=expected_head_oid, page=page, limit=limit, review_id=review_id)
        else:
            result = remote.file_at_commit(repository["remote_path"], commit_oid, file_path)
    except ConnectorFailure as error:
        status = 409 if error.code == "review_head_changed" else 503 if error.transient else 422
        raise DomainError(error.code, "Forge inspection could not be completed", status) from None
    finally:
        if remote is not None:
            remote.close()
    with database.transaction() as conn:
        require_project(conn, authenticate(conn), repository["project_id"])
        if item and item["kind"] == "pull_request":
            from .reviews import review_readiness
            result["review_readiness"] = review_readiness(conn, get(conn, "forge_item", item["id"]))
    return {"repository_id": str(repository["id"]), **result}
