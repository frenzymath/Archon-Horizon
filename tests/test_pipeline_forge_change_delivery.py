from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from archon_horizon.pipeline.integrations.connectors import ConnectorFailure, ConnectorManager, ForgejoClient
from archon_horizon.pipeline.integrations.forge_change_transport import _verify_changes, change_files
from archon_horizon.pipeline.persistence.records import create, get
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_api import api, api_database, mutate


def blob(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class Forge:
    base = "a" * 40
    commit = "c" * 40

    def __init__(self, lost=None):
        self.head = None
        self.lost = lost
        self.old = {"old.md": b"old"}
        self.new = dict(self.old)
        self.message = ""
        self.posts = []

    def handler(self, request):
        path = request.url.path
        if request.method == "POST":
            payload = json.loads(request.content)
            self.posts.append((path, payload))
            if path.endswith("/branches"):
                assert payload["old_ref_name"] == self.base
                assert payload["new_branch_name"].startswith("horizon/changes/")
                self.head = self.base
                result = {"commit": {"id": self.head}}
                stage = "branch"
            else:
                assert payload["branch"].startswith("horizon/changes/")
                for item in payload["files"]:
                    if item["operation"] == "delete":
                        del self.new[item["path"]]
                    else:
                        self.new[item["path"]] = base64.b64decode(item["content"])
                self.head, self.message = self.commit, payload["message"]
                result, stage = {"commit": {"sha": self.head}}, "commit"
            if self.lost == stage:
                self.lost = None
                raise httpx.ReadTimeout("Mutation committed, response lost", request=request)
            return httpx.Response(201, json=result)
        if "/branches/" in path:
            return httpx.Response(200, json={"commit": {"id": self.head}}) if self.head else httpx.Response(404)
        oid = path.rsplit("/", 1)[-1]
        if "/git/commits/" in path:
            return httpx.Response(200, json={"sha": oid, "commit": {"message": "Base" if oid == self.base else self.message,
                "tree": {"sha": oid}}, "parents": [] if oid == self.base else [{"sha": self.base}]})
        assert "/git/trees/" in path
        data = self.old if oid == self.base else self.new
        items = [{"path": name, "mode": "100644", "type": "blob", "sha": blob(content)} for name, content in sorted(data.items())]
        # Forgejo 16 can report truncated=true on its last page; stable total_count is authoritative.
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"tree": items[page - 1:page], "truncated": True,
                                        "page": page, "total_count": len(items)})


@pytest.mark.parametrize("lost", ["branch", "commit"])
def test_change_lost_ack_reconciles_before_retry(lost):
    forge = Forge(lost)
    remote = ForgejoClient("https://forge.invalid", "secret", client=httpx.Client(transport=httpx.MockTransport(forge.handler)))
    args = {"base_commit_oid": forge.base, "message": "Reviewed milestone", "operation_id": str(uuid4()),
            "files": [{"operation": "create", "path": "new.md", "content": base64.b64encode(b"new").decode()},
                      {"operation": "delete", "path": "old.md", "sha": blob(b"old")}]}
    with pytest.raises(ConnectorFailure) as failed:
        change_files(remote, "owner/library", **args)
    assert failed.value.uncertain
    if lost == "branch":
        with pytest.raises(ConnectorFailure, match="reauthorize") as repair:
            change_files(remote, "owner/library", **args, reconcile_only=True)
        assert not repair.value.uncertain and len(forge.posts) == 1
        result = change_files(remote, "owner/library", **args)
    else:
        result = change_files(remote, "owner/library", **args, reconcile_only=True)
    assert result["commit_oid"] == forge.commit
    assert len(forge.posts) == 2
    assert change_files(remote, "owner/library", **args, reconcile_only=True) == result
    assert len(forge.posts) == 2
    forge.new["unexpected.md"] = b"unreviewed"
    with pytest.raises(ConnectorFailure, match="conflicts"):
        change_files(remote, "owner/library", **args, reconcile_only=True)


def test_existing_pr_branch_amendment_reconciles_without_a_new_branch():
    forge = Forge("commit")
    forge.head = forge.base
    remote = ForgejoClient("https://forge.invalid", "secret", client=httpx.Client(transport=httpx.MockTransport(forge.handler)))
    args = dict(base_commit_oid=forge.base, message="Fix the PR", operation_id=str(uuid4()),
        branch="horizon/changes/existing-pr", files=[{"operation":"create","path":"fixed.md",
            "content":base64.b64encode(b"fixed").decode()}])
    with pytest.raises(ConnectorFailure):
        change_files(remote,"owner/library",**args)
    result = change_files(remote,"owner/library",**args,reconcile_only=True)
    assert result["branch"] == args["branch"]
    assert len(forge.posts) == 1 and forge.posts[0][0].endswith("/contents")


def test_existing_pr_branch_refuses_missing_or_changed_base():
    forge = Forge()
    remote = ForgejoClient("https://forge.invalid", "secret", client=httpx.Client(transport=httpx.MockTransport(forge.handler)))
    args = dict(base_commit_oid=forge.base,message="Fix",operation_id=str(uuid4()),branch="horizon/changes/existing",
        files=[{"operation":"create","path":"fixed.md","content":base64.b64encode(b"fixed").decode()}])
    with pytest.raises(ConnectorFailure,match="missing"):
        change_files(remote,"owner/library",**args)
    forge.head = forge.commit
    with pytest.raises(ConnectorFailure,match="conflicts"):
        change_files(remote,"owner/library",**args)
    assert not forge.posts


@pytest.mark.parametrize("remote_path", ["owner/library", None])
def test_change_dispatch_records_verified_artifact_and_location(api, remote_path):
    client, database, world, run, token, _ = api
    repo_id = world.document["source_repository_id"]
    with database.transaction() as conn:
        existing = get(conn, "repository", repo_id)
        repository = create(conn, "repository", project_id=world.project["id"], slug="review-library", integration_id=existing["integration_id"],
                            remote_id="owner/library", remote_path=remote_path, default_branch="main", purpose="library")
        repo_id = repository["id"]
    artifact = mutate(client, "/api/v3/artifacts", {"project_id": str(world.project["id"]),
        "content_base64": base64.b64encode(b"milestone").decode(), "media_type": "text/plain"}, token).json()
    proposal = mutate(client, "/api/v3/forge/change", {"repository_id": str(repo_id), "origin_run_id": str(run["id"]),
        "base_commit_oid": Forge.base, "message": "State milestone", "files": [{"operation": "create", "path": "new.md",
        "content_artifact_id": artifact["id"]}]}, token)
    assert proposal.status_code == 200, proposal.text
    operation_id = proposal.json()["operation"]["id"]
    forge = Forge()
    manager = ConnectorManager(database, world.service, lambda ref: {"token": "private"},
        client_factory=lambda integration: httpx.Client(transport=httpx.MockTransport(forge.handler)))
    assert manager.dispatch_one() == "completed"
    with database.transaction() as conn:
        operation = get(conn, "outbox_operation", operation_id)
        reference = get(conn, "object_reference", operation["result_ref_id"])
        artifact = get(conn, "artifact", reference["artifact_id"])
        assert artifact["content"] == {"repository_id": str(repo_id), "commit_oid": forge.commit}
        location = conn.execute(select(tables["artifact_location"]).where(tables["artifact_location"].c.artifact_id == artifact["id"])).mappings().one()
        assert location["verified_at"] and location["locator"].endswith("/owner/library/commit/" + forge.commit)


def test_change_verification_skips_unchanged_subtrees():
    calls = []
    trees = {
        "old": [{"path": "Mathlib", "type": "tree", "mode": "040000", "sha": "large-unchanged"},
                {"path": "Roadmap", "type": "tree", "mode": "040000", "sha": "old-subtree"}],
        "new": [{"path": "Mathlib", "type": "tree", "mode": "040000", "sha": "large-unchanged"},
                {"path": "Roadmap", "type": "tree", "mode": "040000", "sha": "new-subtree"}],
        "old-subtree": [{"path": "old.md", "type": "blob", "mode": "100644", "sha": blob(b"old")}],
        "new-subtree": [{"path": "new.md", "type": "blob", "mode": "100644", "sha": blob(b"new")}],
    }

    class Remote:
        def request(self, method, path, *, params):
            oid = path.rsplit("/", 1)[-1]
            calls.append(oid)
            assert params["recursive"] == "false"
            return {"tree": trees[oid], "total_count": len(trees[oid]), "page": 1}

    _verify_changes(Remote(), "/repo", {"tree": {"sha": "old"}}, {"tree": {"sha": "new"}},
                    {"Roadmap/old.md": ("delete", blob(b"old"), None),
                     "Roadmap/new.md": ("create", None, blob(b"new"))})
    assert calls == ["old", "new", "old-subtree", "new-subtree"]


@pytest.mark.parametrize(("operation", "name", "old_oid", "code"), [
    ("create", "old.md", None, "change_create_path_exists"),
    ("update", "old.md", "b" * 40, "change_base_blob_mismatch"),
    ("delete", "old.md", "b" * 40, "change_base_blob_mismatch"),
    ("update", "missing.md", "b" * 40, "change_base_path_missing"),
    ("delete", "missing/sub/file.md", "b" * 40, "change_base_path_missing"),
    ("create", "old.md/child.md", None, "change_base_parent_not_directory"),
])
@pytest.mark.parametrize("branch_exists", [False, True])
def test_change_preflight_rejects_base_conflicts_before_any_mutation(operation, name, old_oid, code, branch_exists):
    forge = Forge()
    forge.head = forge.base if branch_exists else None
    file = {"operation": operation, "path": name}
    if old_oid:
        file["sha"] = old_oid
    if operation != "delete":
        file["content"] = base64.b64encode(b"replacement").decode()
    remote = ForgejoClient("https://forge.invalid", "secret", client=httpx.Client(transport=httpx.MockTransport(forge.handler)))
    with pytest.raises(ConnectorFailure) as failed:
        change_files(remote, "owner/library", base_commit_oid=forge.base, message="Change", files=[file], operation_id=str(uuid4()))
    assert failed.value.code == code
    assert name in str(failed.value) and forge.base in str(failed.value)
    assert not failed.value.uncertain and not failed.value.transient
    assert not forge.posts
    assert forge.new == forge.old


def test_change_preflight_rejects_overlapping_new_paths():
    forge = Forge()
    remote = ForgejoClient("https://forge.invalid", "secret", client=httpx.Client(transport=httpx.MockTransport(forge.handler)))
    with pytest.raises(ConnectorFailure, match="change_paths_overlap"):
        change_files(remote, "owner/library", base_commit_oid=forge.base, message="Change", operation_id=str(uuid4()),
                     files=[{"operation": "create", "path": name, "content": base64.b64encode(b"new").decode()}
                            for name in ("missing/child.md", "missing/child.md/nested.md")])
    assert not forge.posts


def test_change_preflight_malformed_tree_is_not_an_uncertain_write():
    forge = Forge()

    def handler(request):
        if "/git/trees/" in request.url.path:
            return httpx.Response(200, json={"tree": []})
        return forge.handler(request)

    remote = ForgejoClient("https://forge.invalid", "secret", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ConnectorFailure) as failed:
        change_files(remote, "owner/library", base_commit_oid=forge.base, message="Change", operation_id=str(uuid4()),
                     files=[{"operation": "create", "path": "new.md", "content": base64.b64encode(b"new").decode()}])
    assert failed.value.code == "commit_tree_not_fully_observed"
    assert not failed.value.uncertain and not forge.posts


class NestedForge(Forge):
    def __init__(self):
        super().__init__()
        self.old = {"Proofs/old.lean": b"old", "Proofs/remove.lean": b"remove", "Mathlib/Untouched.lean": b"large"}
        self.new = dict(self.old)
        self.tree_reads = []

    def handler(self, request):
        if "/git/trees/" not in request.url.path:
            return super().handler(request)
        trees = {}

        def build(files):
            entries = []
            directories = {}
            for name, content in sorted(files.items()):
                first, separator, rest = name.partition("/")
                if separator:
                    directories.setdefault(first, {})[rest] = content
                else:
                    entries.append({"path": first, "mode": "100644", "type": "blob", "sha": blob(content)})
            for name, children in directories.items():
                entries.append({"path": name, "mode": "040000", "type": "tree", "sha": build(children)})
            oid = blob(json.dumps(entries, sort_keys=True).encode())
            trees[oid] = entries
            return oid

        trees[self.base] = trees[build(self.old)]
        trees[self.commit] = trees[build(self.new)]
        oid = request.url.path.rsplit("/", 1)[-1]
        self.tree_reads.append(oid)
        assert request.url.params["recursive"] == "false"
        return httpx.Response(200, json={"tree": trees[oid], "total_count": len(trees[oid]), "page": 1})


def test_change_preflight_nested_paths_share_cached_trees_with_verification():
    forge = NestedForge()
    remote = ForgejoClient("https://forge.invalid", "secret", client=httpx.Client(transport=httpx.MockTransport(forge.handler)))
    result = change_files(remote, "owner/library", base_commit_oid=forge.base, message="Change", operation_id=str(uuid4()), files=[
        {"operation": "update", "path": "Proofs/old.lean", "sha": blob(b"old"), "content": base64.b64encode(b"updated").decode()},
        {"operation": "delete", "path": "Proofs/remove.lean", "sha": blob(b"remove")},
        {"operation": "create", "path": "Proofs/New/result.lean", "content": base64.b64encode(b"new").decode()},
    ])
    assert result["commit_oid"] == forge.commit
    assert forge.new == {"Proofs/old.lean": b"updated", "Proofs/New/result.lean": b"new", "Mathlib/Untouched.lean": b"large"}
    # Old root and Proofs, new root and Proofs, new New; Mathlib is never traversed.
    assert len(forge.tree_reads) == len(set(forge.tree_reads)) == 5
    assert len(forge.posts) == 2


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
def test_change_preflight_rejects_directory_as_file(operation):
    forge = NestedForge()
    remote = ForgejoClient("https://forge.invalid", "secret", client=httpx.Client(transport=httpx.MockTransport(forge.handler)))
    file = {"operation": operation, "path": "Proofs"}
    if operation != "create":
        file["sha"] = "b" * 40
    if operation != "delete":
        file["content"] = base64.b64encode(b"replacement").decode()
    with pytest.raises(ConnectorFailure) as failed:
        change_files(remote, "owner/library", base_commit_oid=forge.base, message="Change", operation_id=str(uuid4()), files=[file])
    assert failed.value.code == ("change_create_path_exists" if operation == "create" else "change_base_path_not_file")
    assert not forge.posts


def test_disposable_forgejo_real_branch_bulk_change_and_readback():
    access_path = os.environ.get("HORIZON_TEST_FORGEJO_ACCESS")
    if not access_path:
        pytest.skip("explicit disposable Forgejo access file required")
    access = json.loads(Path(access_path).read_text())
    assert access["base_url"] == "http://127.0.0.1:55441"
    remote = ForgejoClient(access["base_url"], access["token"])
    try:
        base = remote.request("GET", "/api/v1/repos/pipeline-test/protocol/branches/main")["commit"]["id"]
        args = {"base_commit_oid": base, "message": "Horizon broker protocol validation", "operation_id": str(uuid4()),
                "files": [{"operation": "create", "path": "Broker/Proof.lean",
                           "content": base64.b64encode(b"theorem broker_proof : True := True.intro\n").decode()}]}
        result = change_files(remote, "pipeline-test/protocol", **args)
        assert result == change_files(remote, "pipeline-test/protocol", **args, reconcile_only=True)
        content = remote.file_at_commit("pipeline-test/protocol", result["commit_oid"], "Broker/Proof.lean")
        assert content["text"].startswith("theorem broker_proof")
        pull = remote.create_item("pipeline-test/protocol", kind="pull_request", title="Broker protocol validation",
                                  body="Disposable integration test", operation_id=str(uuid4()),
                                  head=result["branch"], base="main")
        remote.comment("pipeline-test/protocol", pull["number"], body="Scoped review readback", operation_id=str(uuid4()))
        for view in ("files", "diff", "comments"):
            inspected = remote.inspect_item("pipeline-test/protocol", pull["number"], kind="pull_request",
                                            view=view, expected_head_oid=result["commit_oid"], page=1, limit=100)
            assert inspected["head_commit_oid"] == result["commit_oid"]
            assert inspected.get("items") or inspected.get("diff")
        assert remote.request("GET", "/api/v1/repos/pipeline-test/protocol/branches/main")["commit"]["id"] == base
    finally:
        remote.close()
