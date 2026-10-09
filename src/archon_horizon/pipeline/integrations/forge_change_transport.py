"""Create an isolated review branch using Forgejo's atomic multi-file API."""

from __future__ import annotations

import base64
import hashlib
import time
from urllib.parse import quote
from uuid import UUID

from .connectors import ConnectorFailure


def _commit(remote, path, oid):
    value = remote.request("GET", f"{path}/git/commits/{oid}")
    details = value.get("commit", value) if isinstance(value, dict) else {}
    if not isinstance(value, dict) or value.get("sha") != oid or not isinstance(details.get("tree"), dict):
        raise ConnectorFailure("malformed_git_commit")
    return {**value, "message": details.get("message", ""), "tree": details["tree"]}


def _tree(remote, path, tree_ref, budget, *, cache=None, uncertain=True):
    if cache is not None and tree_ref in cache:
        return cache[tree_ref]
    result = {}
    total = None
    for page in range(1, 101):
        budget[0] += 1
        if budget[0] > 512 or time.monotonic() > budget[1]:
            raise ConnectorFailure("change_verification_budget_exceeded", uncertain=uncertain)
        value = remote.request("GET", f"{path}/git/trees/{tree_ref}",
                               params={"recursive": "false", "page": page, "per_page": 1000})
        if (not isinstance(value, dict) or not isinstance(value.get("tree"), list)
                or not isinstance(value.get("total_count"), int) or value.get("page", page) != page):
            raise ConnectorFailure("commit_tree_not_fully_observed", uncertain=uncertain)
        if total is None:
            total = value["total_count"]
        if value["total_count"] != total or not 0 <= total <= 10000:
            raise ConnectorFailure("commit_tree_count_changed_or_oversized", uncertain=uncertain)
        for item in value["tree"]:
            if item.get("path") in result or not isinstance(item.get("path"), str) or "/" in item["path"]:
                raise ConnectorFailure("malformed_or_oversized_git_tree", uncertain=uncertain)
            if item.get("type") not in {"tree", "blob", "commit"}:
                raise ConnectorFailure("malformed_git_tree", uncertain=uncertain)
            result[item["path"]] = (item["type"], item["mode"], item["sha"])
        if len(result) == total:
            if cache is not None:
                cache[tree_ref] = result
            return result
        if not value["tree"]:
            break
    raise ConnectorFailure("commit_tree_pagination_limit", uncertain=uncertain)


def _validate_base_changes(remote, path, base, expected, cache):
    budget = [0, time.monotonic() + 120]
    for name, (action, old_oid, _) in expected.items():
        parts = name.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if parent in expected:
                raise ConnectorFailure("change_paths_overlap", message=(
                    f"File operations overlap at {parent!r} and {name!r}; submit non-overlapping file paths."))
        tree_ref = base["tree"]["sha"]
        entry = None
        for index, part in enumerate(parts):
            relative = "/".join(parts[:index + 1])
            tree = _tree(remote, path, tree_ref, budget, cache=cache, uncertain=False)
            entry = tree.get(part)
            if index == len(parts) - 1 or entry is None:
                break
            if entry[0] != "tree":
                raise ConnectorFailure("change_base_parent_not_directory", message=(
                    f"Cannot {action} {name!r}: parent {relative!r} is not a directory at base {base['sha']}."))
            tree_ref = entry[2]
        if action == "create":
            if entry is not None:
                raise ConnectorFailure("change_create_path_exists", message=(
                    f"Cannot create {name!r}: it already exists at base {base['sha']} "
                    f"({entry[0]} {entry[2]}). Read that version and use an update with its blob SHA if intended."))
        elif entry is None:
            raise ConnectorFailure("change_base_path_missing", message=(
                f"Cannot {action} {name!r}: it does not exist at base {base['sha']}. "
                "Read the exact base tree and correct the operation or base commit."))
        elif entry[0] != "blob" or entry[1] not in {"100644", "100755"}:
            raise ConnectorFailure("change_base_path_not_file", message=(
                f"Cannot {action} {name!r}: it is not a regular file at base {base['sha']}."))
        elif entry[2] != old_oid:
            raise ConnectorFailure("change_base_blob_mismatch", message=(
                f"Cannot {action} {name!r}: supplied blob SHA {old_oid} differs from "
                f"{entry[2]} at base {base['sha']}. Read that version before preparing the change."))


def _verify_changes(remote, path, base, commit, expected, *, tree_cache=None):
    pending = [("", base["tree"]["sha"], commit["tree"]["sha"])]
    remaining = dict(expected)
    budget = [0, time.monotonic() + 120]
    while pending:
        prefix, old_ref, new_ref = pending.pop()
        if old_ref == new_ref:
            continue
        old_tree = _tree(remote, path, old_ref, budget, cache=tree_cache) if old_ref else {}
        new_tree = _tree(remote, path, new_ref, budget, cache=tree_cache) if new_ref else {}
        for name in old_tree.keys() | new_tree.keys():
            old, new = old_tree.get(name), new_tree.get(name)
            if old == new:
                continue
            relative = prefix + name
            old_dir, new_dir = bool(old and old[0] == "tree"), bool(new and new[0] == "tree")
            if old_dir or new_dir:
                if not any(target.startswith(relative + "/") for target in expected):
                    raise ConnectorFailure("change_branch_outcome_conflicts", uncertain=True)
                pending.append((relative + "/", old[2] if old_dir else None, new[2] if new_dir else None))
                if old_dir and new_dir:
                    continue
            before, after = None if old_dir else old, None if new_dir else new
            if before is None and after is None:
                continue
            target = remaining.pop(relative, None)
            if target is None:
                raise ConnectorFailure("change_branch_outcome_conflicts", uncertain=True)
            action, old_oid, new_oid = target
            if ((before is not None and (before[0] != "blob" or before[1] not in {"100644", "100755"} or before[2] != old_oid))
                    or (before is None and action != "create")
                    or (after is None and action != "delete")
                    or (after is not None and (after[0] != "blob" or after[1] != (before[1] if before else "100644") or after[2] != new_oid))):
                raise ConnectorFailure("change_branch_outcome_conflicts", uncertain=True)
    if remaining:
        raise ConnectorFailure("change_branch_outcome_conflicts", uncertain=True)


def change_files(remote, remote_path, *, base_commit_oid, message, files, operation_id, reconcile_only=False, branch=None):
    identifier = str(UUID(operation_id))
    existing_branch = branch is not None
    branch = branch or "horizon/changes/" + identifier
    marker = "Horizon-Operation: " + identifier
    path = remote.repository_path(remote_path)
    branch_path = f"{path}/branches/{quote(branch, safe='')}"
    base = _commit(remote, path, base_commit_oid)
    expected = {}
    for file in files:
        name, action = file["path"], file["operation"]
        old_oid = file.get("sha")
        if action == "delete":
            new_oid = None
        else:
            data = base64.b64decode(file["content"], validate=True)
            digest = hashlib.sha1 if len(base_commit_oid) == 40 else hashlib.sha256
            new_oid = digest(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            if old_oid == new_oid:
                raise ConnectorFailure("change_update_is_noop")
        expected[name] = (action, old_oid, new_oid)
    try:
        observed = remote.request("GET", branch_path)
    except ConnectorFailure as error:
        if error.code != "http_404":
            raise
        observed = None
    tree_cache = {}
    observed_head = observed.get("commit", {}).get("id") if isinstance(observed, dict) else None
    if not reconcile_only and (observed is None or observed_head == base_commit_oid):
        _validate_base_changes(remote, path, base, expected, tree_cache)
    if observed is None:
        if existing_branch:
            raise ConnectorFailure("amendment_branch_missing")
        if reconcile_only:
            raise ConnectorFailure("change_branch_not_observed_reauthorize_retry")
        remote.request("POST", path + "/branches", body={"new_branch_name": branch, "old_ref_name": base_commit_oid}, mutating=True)
        observed = remote.request("GET", branch_path)
    head = observed.get("commit", {}).get("id") if isinstance(observed, dict) else None
    if not head:
        raise ConnectorFailure("change_branch_head_missing", uncertain=True)
    if head == base_commit_oid:
        if reconcile_only:
            raise ConnectorFailure("change_branch_prepared_without_commit_reauthorize_retry")
        remote.request("POST", path + "/contents", body={"branch": branch, "files": files,
                       "message": message.rstrip() + "\n\n" + marker}, mutating=True)
        observed = remote.request("GET", branch_path)
        head = observed.get("commit", {}).get("id") if isinstance(observed, dict) else None
    if not head or head == base_commit_oid:
        raise ConnectorFailure("change_commit_not_observed", uncertain=True)
    commit = _commit(remote, path, head)
    if (marker not in commit.get("message", "").splitlines()
            or [item.get("sha") for item in commit.get("parents", [])] != [base_commit_oid]):
        raise ConnectorFailure("change_branch_outcome_conflicts", uncertain=True)
    _verify_changes(remote, path, base, commit, expected, tree_cache=tree_cache)
    return {"branch": branch, "commit_oid": head, "base_commit_oid": base_commit_oid}
