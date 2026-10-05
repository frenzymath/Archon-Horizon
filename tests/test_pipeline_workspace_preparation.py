from dataclasses import replace
import json
from pathlib import Path
import subprocess
from uuid import uuid4

import httpx
import pytest

from archon_horizon.pipeline.worker.contracts import ExecutionGrant, FencedExecution
from archon_horizon.pipeline.worker.workspaces import prepare_workspace
from archon_horizon.pipeline.worker.daemon import HarnessConfig, WorkerDaemon
from archon_horizon.pipeline.worker.journal import DurableJournal
from archon_horizon.pipeline.worker.provider import HeadlessAdapter
from archon_horizon.pipeline.worker.transport import WorkerTransport


def git(path, *args):
    return subprocess.check_output(["git", "-c", "user.name=Horizon test", "-c", "user.email=test@example.invalid",
                                    "-c", "commit.gpgsign=false", "-C", str(path), *args], text=True).strip()


@pytest.fixture
def preparation(tmp_path):
    root = tmp_path / "workspaces"
    source = root / "source"
    source.mkdir(parents=True)
    git(source, "init", "-q")
    (source / "proof.lean").write_text("theorem test : True := trivial\n")
    git(source, "add", "proof.lean")
    git(source, "commit", "-qm", "source")
    commit = git(source, "rev-parse", "HEAD")
    assignment, workspace = str(uuid4()), str(uuid4())
    grant = ExecutionGrant(execution_id=str(uuid4()), assignment_id=assignment, epoch=1, lease_seconds=60,
        harness_id=str(uuid4()), workspace_id=workspace, workspace_path=str(root / "assignments" / assignment),
        repository_id=str(uuid4()), goal="work", workspace_preparation={"schema_version": 1,
            "source_workspace_id": str(uuid4()), "source_path": str(source), "commit_oid": commit,
            "branch_name": "horizon/assignments/" + assignment})
    return grant, root, source, tmp_path / "state"


def test_isolated_worktree_uses_pinned_objects_without_dirty_files_or_caches(preparation):
    grant, root, source, state = preparation
    committed = (source / "proof.lean").read_text()
    (source / "proof.lean").write_text("unpublished parent edits\n")
    (source / ".lake").mkdir()
    (source / ".lake" / "cache").write_text("large cache")
    heartbeats = []
    receipt = prepare_workspace(grant, (root,), state, lambda: heartbeats.append(True))
    destination = Path(grant.workspace_path)
    assert (destination / "proof.lean").read_text() == committed
    assert not (destination / ".lake").exists()
    assert (source / "proof.lean").read_text() == "unpublished parent edits\n"
    assert (source / "proof.lean").stat().st_ino != (destination / "proof.lean").stat().st_ino
    assert git(source, "rev-parse", "--path-format=absolute", "--git-common-dir") == git(
        destination, "rev-parse", "--path-format=absolute", "--git-common-dir")
    assert receipt["head_commit_oid"] == grant.workspace_preparation["commit_oid"]
    assert len(heartbeats) >= 2
    assert prepare_workspace(grant, (root,), state, lambda: None) == receipt


def test_existing_unrecorded_directory_is_never_adopted(preparation):
    grant, root, source, state = preparation
    destination = Path(grant.workspace_path)
    destination.mkdir(parents=True)
    (destination / "important").write_text("keep")
    with pytest.raises(ValueError, match="unrecorded"):
        prepare_workspace(grant, (root,), state, lambda: None)
    assert (destination / "important").read_text() == "keep"


def test_retry_preserves_unpublished_changes_in_prepared_worktree(preparation):
    grant, root, source, state = preparation
    prepare_workspace(grant, (root,), state, lambda: None)
    destination = Path(grant.workspace_path)
    (destination / "proof.lean").write_text("new work")
    with pytest.raises(ValueError, match="contains changes"):
        prepare_workspace(grant, (root,), state, lambda: None)
    assert (destination / "proof.lean").read_text() == "new work"


@pytest.mark.parametrize("stage", ["branch", "partial_checkout", "renamed"])
def test_interrupted_preparation_resumes_owned_staging_worktree(preparation, stage):
    grant, root, source, state = preparation
    private = state / "workspace-preparations"
    private.mkdir(parents=True)
    (private / (grant.workspace_id + ".json")).write_text(json.dumps({
        "workspace_id": grant.workspace_id, "path": grant.workspace_path, **grant.workspace_preparation}))
    branch, commit = grant.workspace_preparation["branch_name"], grant.workspace_preparation["commit_oid"]
    staged = root / "assignments" / (".horizon-preparing-" + grant.workspace_id)
    staged.parent.mkdir()
    if stage == "branch":
        git(source, "branch", branch, commit)
    else:
        git(source, "worktree", "add", "--no-checkout", "--lock", "-b", branch, str(staged), commit)
        if stage == "partial_checkout":
            (staged / "proof.lean").write_text("partial interrupted checkout")
        else:
            git(staged, "read-tree", "--reset", "-u", commit)
            staged.rename(grant.workspace_path)
    recorded = []
    receipt = prepare_workspace(grant, (root,), state, lambda: None, recorded.append)
    assert receipt["head_commit_oid"] == commit
    assert (Path(grant.workspace_path) / "proof.lean").read_text() == (source / "proof.lean").read_text()
    assert not staged.exists()
    assert any(item.get("pid") and item.get("process_identity") and item.get("boot_id") for item in recorded)
    assert recorded[-1]["pid"] is None


@pytest.mark.parametrize("change", ["escape", "symlink", "branch", "commit", "nested", "unknown"])
def test_invalid_preparation_is_rejected_before_checkout(preparation, change):
    grant, root, source, state = preparation
    manifest = dict(grant.workspace_preparation)
    if change == "escape":
        grant = replace(grant, workspace_path=str(root.parent / "assignments" / grant.assignment_id))
    elif change == "symlink":
        (root / "redirect").symlink_to(source, target_is_directory=True)
        manifest["source_path"] = str(root / "redirect")
    elif change == "branch":
        manifest["branch_name"] = "main"
    elif change == "commit":
        manifest["commit_oid"] = "HEAD"
    elif change == "nested":
        grant = replace(grant, workspace_path=str(source / "not-assignment"))
    else:
        manifest["shell"] = "not allowed"
    grant = replace(grant, workspace_preparation=manifest)
    with pytest.raises(ValueError):
        prepare_workspace(grant, (root,), state, lambda: None)
    assert not (root / "assignments").exists()


def test_expired_lease_cannot_create_a_worktree(preparation):
    grant, root, source, state = preparation
    def expired():
        raise FencedExecution("expired")
    with pytest.raises(FencedExecution):
        prepare_workspace(grant, (root,), state, expired)
    assert not Path(grant.workspace_path).exists()


@pytest.mark.parametrize("stop", [False, True])
def test_claim_prepares_and_acknowledges_before_provider_launch(preparation, monkeypatch, stop):
    grant, root, source, state = preparation
    events = []
    def handler(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"execution": grant.__dict__})
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"lease_seconds": 60, "stop": stop})
        events.append(json.loads(request.content))
        return httpx.Response(200, json={"acknowledged": True})
    transport = WorkerTransport("http://testserver", "host-secret", client=httpx.Client(transport=httpx.MockTransport(handler)))
    journal = DurableJournal(state, minimum_free_bytes=0)
    config = HarnessConfig(HeadlessAdapter("codex_exec", "/bin/true"), root.parent / "provider",
                           root.parent / "scratch", unrestricted=True)
    daemon = WorkerDaemon(host_id=str(uuid4()), journal=journal, transport=transport,
        harnesses={grant.harness_id: config}, workspace_roots=(root,))
    launched = []
    def execute(actual, **kwargs):
        assert Path(actual.workspace_path).is_dir()
        assert any(json.loads(row["envelope"])["kind"] == "workspace_prepared"
                   and row["state"] == "acknowledged" for row in journal.records())
        launched.append(actual)
        return "succeeded"
    monkeypatch.setattr(daemon, "execute", execute)
    try:
        assert daemon.run_once() == ("lost" if stop else "succeeded")
        assert bool(launched) is not stop
        if stop:
            assert not Path(grant.workspace_path).exists()
    finally:
        transport.close()
        journal.close()
