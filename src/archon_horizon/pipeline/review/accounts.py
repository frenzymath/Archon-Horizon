"""Forge identities for reviewer descriptors and scoped execution credentials."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx
from sqlalchemy import select

from ..auth import live_execution
from ..integrations.connectors import ConnectorFailure, ForgejoClient, RemoteClient, SecretResolver
from ..errors import DomainError
from ..persistence.records import change, create, emit, get, snapshot, transaction_lock
from ..persistence.schema import tables


def _descriptors(conn, project_id):
    descriptor, policy, repository, policy_repository = (
        tables[name] for name in ("reviewer_descriptor", "review_policy", "repository", "review_policy_repository"))
    rows = conn.execute(select(descriptor, repository.c.id.label("repository_id"),
        repository.c.integration_id, repository.c.remote_path).select_from(descriptor).join(
        policy, policy.c.project_id == descriptor.c.project_id).join(
        policy_repository, policy_repository.c.review_policy_id == policy.c.id).join(
        repository, repository.c.id == policy_repository.c.repository_id).where(
        descriptor.c.project_id == project_id, descriptor.c.enabled.is_(True),
        policy.c.project_id == project_id, policy.c.enabled.is_(True)))
    result = {}
    for row in rows.mappings():
        value = result.setdefault(row["id"], {**dict(row), "repositories": {}})
        value["repositories"][row["repository_id"]] = {
            "integration_id": row["integration_id"], "remote_path": row["remote_path"]}
    return list(result.values())


def credentials(conn, actor, execution_id, service, resolver=None):
    """Return secrets only to their live owning execution, never an operator view."""
    execution = live_execution(conn, actor)
    if str(execution["id"]) != str(execution_id):
        raise DomainError("forbidden", "Reviewer credentials belong to another execution", 403)
    assignment = get(conn, "assignment", execution["assignment_id"])
    resolver = resolver or SecretResolver(service.config.state_root)
    accounts = []
    for descriptor in _descriptors(conn, execution["project_id"]):
        if execution["role"] != "maintainer" and assignment["reviewer_descriptor_id"] != descriptor["id"]:
            continue
        identity_id = descriptor["integration_identity_id"]
        if identity_id is None:
            continue
        identity = get(conn, "integration_identity", identity_id)
        integration = get(conn, "integration", identity["integration_id"])
        if not identity["enabled"] or not integration["enabled"]:
            continue
        repositories = [repo["remote_path"] for repo in descriptor["repositories"].values()
                        if repo["integration_id"] == integration["id"]]
        if not repositories:
            continue
        secret = resolver(identity["credential_ref"])
        if not secret.get("token"):
            raise ConnectorFailure("reviewer_token_missing")
        principal = get(conn, "principal", identity["principal_id"])
        public_urls = getattr(service.config, "integration_public_urls", {})
        endpoint = public_urls.get(integration["id"], public_urls.get(str(integration["id"]), integration["endpoint"]))
        accounts.append({"descriptor_id": str(descriptor["id"]), "slug": descriptor["slug"],
            "display_name": principal["display_name"], "integration_identity_id": str(identity_id),
            "integration_id": str(integration["id"]), "endpoint": str(endpoint).rstrip("/"),
            "username": secret.get("username") or principal.get("service_name"),
            "remote_user_id": identity["remote_user_id"], "token": secret["token"],
            "repository_paths": sorted(set(repositories))})
    return {"execution_id": str(execution_id), "accounts": accounts}


def _write_secret(path: Path, value: dict) -> None:
    """Replace a private secret atomically; credentials never enter DB snapshots."""
    fd, temporary = tempfile.mkstemp(prefix=".reviewer-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _provision(integration, descriptor, resolver, admin_reference, *, client_factory=ForgejoClient):
    for repository in descriptor["repositories"].values():
        if not isinstance(repository["remote_path"], str):
            raise DomainError("reviewer_repository_unconfigured", "Reviewer repository needs its Forge owner/name path", 422)
        ForgejoClient.repository_path(repository["remote_path"])
    identifier = descriptor["id"].hex
    reference = "reviewer-" + identifier
    path = resolver.root / (reference + ".json")
    slug = re.sub(r"[^a-z0-9-]+", "-", descriptor["slug"].lower()).strip("-")[:16].rstrip("-") or "reviewer"
    # Reviewer identity is part of the public review trail.  New accounts use
    # the descriptor slug directly so Forge discussions remain readable.  A
    # previously provisioned account keeps its old login from the private
    # secret; silently changing it would orphan its token and review history.
    previous_username = None
    if path.exists():
        try:
            previous_username = resolver(reference).get("username")
        except (OSError, ValueError, KeyError):
            previous_username = None
    username = previous_username or "horizon-review-" + slug
    email = username + "@horizon.invalid"
    if path.exists():
        secret = resolver(reference)
        if secret.get("username") != username or not secret.get("password"):
            raise ConnectorFailure("reviewer_provisioning_secret_conflict")
    else:
        secret = {"username": username, "password": secrets.token_urlsafe(40)}
        _write_secret(path, secret)
    admin = client_factory(integration["endpoint"], resolver(admin_reference).get("token", ""))
    try:
        try:
            user = admin.request("GET", "/api/v1/users/" + quote(username, safe=""))
        except ConnectorFailure as error:
            if error.code != "http_404":
                raise
            user = admin.request("POST", "/api/v1/admin/users", body={
                "username": username, "email": email, "password": secret["password"],
                "full_name": descriptor["slug"].replace("-", " ").title(),
                "must_change_password": False, "restricted": True,
                "send_notify": False, "visibility": "private"}, mutating=True)
        if not isinstance(user, dict) or not user.get("id") or user.get("login") != username:
            raise ConnectorFailure("reviewer_account_identity_mismatch")
        if secret.get("remote_user_id") and secret["remote_user_id"] != str(user["id"]):
            raise ConnectorFailure("reviewer_account_identity_changed")
        secret["remote_user_id"] = str(user["id"])
        if not secret.get("token"):
            # A lost token-creation reply leaves an unusable token. Revoke only
            # this account's managed token name before issuing its replacement.
            token_name = "horizon-reviewer-" + identifier
            token_path = "/api/v1/users/" + quote(username, safe="") + "/tokens"
            owner = RemoteClient(integration["endpoint"], client=admin.client,
                                 auth=httpx.BasicAuth(username, secret["password"]))
            tokens = []
            for page in range(1, 101):
                batch = owner.request("GET", token_path, params={"page": page, "limit": 50})
                if not isinstance(batch, list):
                    raise ConnectorFailure("malformed_forge_token_listing")
                tokens.extend(batch)
                if len(batch) < 50:
                    break
            else:
                raise ConnectorFailure("forge_pagination_limit")
            for token in tokens:
                if token.get("name") == token_name:
                    owner.request("DELETE", token_path + "/" + str(int(token["id"])), mutating=True)
            token = owner.request("POST", token_path, body={"name": token_name,
                "scopes": ["write:repository", "read:user"]}, mutating=True)
            if not isinstance(token, dict) or not isinstance(token.get("sha1"), str) or not token["sha1"]:
                raise ConnectorFailure("malformed_forge_token")
            secret["token"] = token["sha1"]
            _write_secret(path, secret)
        for repository in descriptor["repositories"].values():
            admin.request("PUT", admin.repository_path(repository["remote_path"]) + "/collaborators/" +
                quote(username, safe=""), body={"permission": "read"}, mutating=True)
        return reference, username, str(user["id"])
    finally:
        admin.close()


def _refresh_access(integration, identity, repositories, resolver, admin_reference, client_factory):
    owner = client_factory(integration["endpoint"], resolver(identity["credential_ref"]).get("token", ""))
    try:
        user = owner.request("GET", "/api/v1/user")
        if (not isinstance(user, dict) or str(user.get("id")) != identity["remote_user_id"]
                or not isinstance(user.get("login"), str) or not user["login"]):
            raise ConnectorFailure("reviewer_account_identity_mismatch")
    finally:
        owner.close()
    admin = client_factory(integration["endpoint"], resolver(admin_reference).get("token", ""))
    try:
        for repository in repositories:
            if not isinstance(repository["remote_path"], str):
                raise DomainError("reviewer_repository_unconfigured", "Reviewer repository needs its Forge owner/name path", 422)
            path = admin.repository_path(repository["remote_path"]) + "/collaborators/" + quote(user["login"], safe="")
            try:
                permission = admin.request("GET", path + "/permission")
            except ConnectorFailure as error:
                if error.code != "http_404":
                    raise
                permission = {}
            if not isinstance(permission, dict):
                raise ConnectorFailure("malformed_forge_permission")
            if permission.get("permission") in {"read", "write", "admin", "owner"}:
                continue
            admin.request("PUT", path, body={"permission": "read"}, mutating=True)
    finally:
        admin.close()


def ensure_project_accounts(database, service, project_id, *, resolver=None, client_factory=ForgejoClient,
                            refresh_access=False):
    """Provision missing configured identities, with HTTP outside DB transactions.

    The installation lock serializes secret creation across API processes. A
    remote failure leaves recoverable private state and no half-linked identity.
    Without an explicit admin credential, existing identities remain usable and
    unconfigured descriptors are reported to the caller without privilege guesses.
    """
    resolver = resolver or SecretResolver(service.config.state_root)
    resolver.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = resolver.root / ".reviewer-accounts.lock"
    descriptor_lock = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    result = {"configured": [], "unconfigured": []}
    try:
        fcntl.flock(descriptor_lock, fcntl.LOCK_EX)
        with database.transaction() as conn:
            descriptors = _descriptors(conn, project_id)
            integrations = {key: get(conn, "integration", key) for descriptor in descriptors
                for key in {repo["integration_id"] for repo in descriptor["repositories"].values()}}
            identities = {descriptor["integration_identity_id"]: get(conn, "integration_identity", descriptor["integration_identity_id"])
                          for descriptor in descriptors if descriptor["integration_identity_id"]}
        admin_refs = getattr(service.config, "reviewer_account_admin_credentials", {})
        for descriptor in descriptors:
            if descriptor["integration_identity_id"]:
                identity = identities[descriptor["integration_identity_id"]]
                integration = integrations.get(identity["integration_id"])
                if not identity["enabled"] or not integration or not integration["enabled"]:
                    result["unconfigured"].append(str(descriptor["id"]))
                    continue
                admin_reference = admin_refs.get(integration["id"], admin_refs.get(str(integration["id"])))
                if refresh_access and admin_reference:
                    _refresh_access(integration, identity, [repo for repo in descriptor["repositories"].values()
                        if repo["integration_id"] == integration["id"]], resolver, admin_reference, client_factory)
                result["configured"].append(str(descriptor["id"]))
                continue
            integration_ids = {repo["integration_id"] for repo in descriptor["repositories"].values()}
            if len(integration_ids) != 1:
                raise DomainError("reviewer_identity_integration_mismatch",
                    "A reviewer descriptor needs separate identities for different Forge integrations", 422)
            integration_id = next(iter(integration_ids))
            integration = integrations[integration_id]
            admin_reference = admin_refs.get(integration_id, admin_refs.get(str(integration_id)))
            if not integration["enabled"] or not admin_reference:
                result["unconfigured"].append(str(descriptor["id"]))
                continue
            reference, username, remote_id = _provision(integration, descriptor, resolver, admin_reference,
                                                       client_factory=client_factory)
            with database.transaction() as conn:
                transaction_lock(conn)
                current = get(conn, "reviewer_descriptor", descriptor["id"], lock=True)
                if current["integration_identity_id"]:
                    result["configured"].append(str(descriptor["id"]))
                    continue
                if current["revision"] != descriptor["revision"] or not current["enabled"]:
                    raise DomainError("reviewer_descriptor_changed", "Reviewer changed during account provisioning")
                principal_table = tables["principal"]
                principal = conn.execute(select(principal_table).where(
                    principal_table.c.service_name == username)).mappings().first()
                if principal is None:
                    principal = create(conn, "principal", kind="service", service_name=username,
                                       display_name=descriptor["slug"].replace("-", " ").title())
                identity_table = tables["integration_identity"]
                identity = conn.execute(select(identity_table).where(identity_table.c.integration_id == integration_id,
                    identity_table.c.remote_user_id == remote_id)).mappings().first()
                if identity is None:
                    identity = create(conn, "integration_identity", integration_id=integration_id,
                        principal_id=principal["id"], remote_user_id=remote_id, credential_ref="secret:" + reference)
                elif identity["principal_id"] != principal["id"] or identity["credential_ref"] != "secret:" + reference:
                    raise DomainError("reviewer_identity_conflict", "Forge account is registered to a different identity")
                updated = change(conn, "reviewer_descriptor", current["id"], current["revision"],
                                 integration_identity_id=identity["id"])
                snapshot(conn, "reviewer_descriptor", updated, principal["id"])
                emit(conn, principal["id"], project_id, "reviewer_descriptor", updated,
                     ["integration_identity_id"], note="Provisioned reviewer Forge account")
            result["configured"].append(str(descriptor["id"]))
    finally:
        os.close(descriptor_lock)
    return result
