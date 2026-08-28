"""Static export: sha256 endpoint files + SPA shell injection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.render.static_export import (
    PAGES_WORKFLOW_FILENAME,
    endpoint_key,
    export_static,
    write_pages_workflow,
)
from archon_horizon.server.service import WorkspaceService


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    main(["--root", str(ws), "init", "--no-interactive"])
    main(["--root", str(ws), "inbox", "add", "--kind", "hint", "--body", "x\n\nseed item"])
    return ws


def test_endpoint_key_matches_sha256() -> None:
    assert endpoint_key("/api/state") == hashlib.sha256(b"/api/state").hexdigest()


def test_export_writes_hashed_endpoint_files(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    service = WorkspaceService(ws)
    out = export_static(service, tmp_path / "out")  # no dist -> Python fallback shell

    state_file = out / "data" / "api" / f"{endpoint_key('/api/state')}.json"
    assert state_file.exists()
    assert json.loads(state_file.read_text())["workspace"] == "ws"
    assert (out / "index.html").exists()  # fallback HTML present


def test_export_with_spa_injects_static_marker(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><head></head><body></body></html>", "utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", "utf-8")

    out = export_static(WorkspaceService(ws), tmp_path / "out", dist_dir=dist)
    index = (out / "index.html").read_text()
    assert "__ARCHON_STATIC__" in index           # flipped into static mode
    assert (out / "assets" / "app.js").exists()    # SPA assets copied


def test_static_export_bounds_history_and_skips_session_details(tmp_path: Path) -> None:
    from types import SimpleNamespace

    class FakeService:
        def __init__(self) -> None:
            self.workspace = SimpleNamespace(state_path=tmp_path / "state")
            self.workspace.state_path.mkdir()
            self._last_state_performance = {"session_count": 2}
            self.transcript_rows = [
                {"ref": "runs/old.jsonl", "meta": {"ended_at": "2024-01-01T00:00:00+00:00"}},
                {"ref": "runs/new.jsonl", "meta": {"ended_at": "2024-01-03T00:00:00+00:00"}},
                {"ref": "runs/newer.jsonl", "meta": {"ended_at": "2024-01-04T00:00:00+00:00"}},
            ]
            self.endpoint_args: dict[str, object] = {}
            self.paths: list[str] = []
            self.state_refs: set[str] | None = None

        def transcripts(self) -> list[dict[str, object]]:
            return list(self.transcript_rows)

        def endpoints(self, **kwargs: object) -> list[str]:
            self.endpoint_args = kwargs
            self.paths = [
                "/api/state",
                "/api/performance",
                "/api/transcripts",
                "/api/report?ref=runs/newer.jsonl",
                "/api/transcript?ref=runs/newer.jsonl&limit=120",
            ]
            return self.paths

        def state(self, *, session_refs: set[str]) -> dict[str, object]:
            self.state_refs = session_refs
            return {"workspace": "fake", "runs": [{"session": ref} for ref in sorted(session_refs)]}

        def serve_endpoint(self, path: str) -> object:
            if path.startswith("/api/report"):
                return {"markdown": "report"}
            if path.startswith("/api/transcript"):
                return {"events": [], "before": None, "has_more": False}
            return self._last_state_performance

    service = FakeService()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><head></head><body></body></html>", "utf-8")
    out = export_static(
        service,
        tmp_path / "out",
        dist_dir=dist,
        history_limit=2,
        transcript_page_limit=1,
    )

    assert service.state_refs == {"runs/new.jsonl", "runs/newer.jsonl"}
    assert service.endpoint_args == {
        "session_refs": {"runs/new.jsonl", "runs/newer.jsonl"},
        "include_session_details": False,
        "transcript_page_limit": 1,
    }
    exported = json.loads((out / "data" / "api" / f"{endpoint_key('/api/transcripts')}.json").read_text())
    assert [row["ref"] for row in exported] == ["runs/newer.jsonl", "runs/new.jsonl"]
    marker = (out / "index.html").read_text()
    assert '"historyLimited": true' in marker
    assert not any(path.startswith("/api/session/") for path in service.paths)


def test_pages_workflow_written_with_relative_upload_path(tmp_path: Path) -> None:
    import yaml

    ws = _workspace(tmp_path)
    out = ws / "docs"
    out.mkdir()

    path, status = write_pages_workflow(ws, out)
    assert status == "written"
    assert path == ws / ".github" / "workflows" / PAGES_WORKFLOW_FILENAME
    doc = yaml.safe_load(path.read_text("utf-8"))
    # upload-pages-artifact gets the out dir *relative to the repo root*.
    upload = next(s for j in doc["jobs"].values() for s in j["steps"] if "upload-pages" in str(s.get("uses")))
    assert upload["with"]["path"] == "docs"
    # the GitHub expression survived templating intact (not brace-mangled).
    # The deploy step retries up to 3x, so page_url falls back across attempts.
    assert (
        "${{ steps.deploy-1.outputs.page_url || steps.deploy-2.outputs.page_url"
        " || steps.deploy-3.outputs.page_url }}" in path.read_text("utf-8")
    )

    # Idempotent: a second call leaves the existing file untouched.
    _, status2 = write_pages_workflow(ws, out)
    assert status2 == "exists"


def test_pages_workflow_refuses_out_dir_outside_repo(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    path, status = write_pages_workflow(ws, outside)
    assert status == "outside"
    assert not (ws / ".github").exists()


def test_static_export_is_committed_for_publish(tmp_path: Path) -> None:
    # Prefer the user root `.git` (what gets pushed to GitHub / Pages). Fall back
    # to the Horizon ledger only when there is no user repo.
    import subprocess

    import pytest

    from archon_horizon.vcs.git import WorkspaceGit, git_available, user_repo_git_dir

    if not git_available():
        pytest.skip("git not installed")

    ws = _workspace(tmp_path)
    main(["--root", str(ws), "dashboard", "--static", "--out", "dashboard", "--workflow"])

    if user_repo_git_dir(ws) is not None:
        tracked = subprocess.run(
            ["git", "ls-files", "dashboard", ".github"],
            cwd=ws,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    else:
        git = WorkspaceGit(ws)
        assert git.is_repo()
        tracked = subprocess.run(
            ["git", "--git-dir", str(git.git_dir), "--work-tree", str(ws),
             "ls-files", "dashboard", ".github"],
            capture_output=True, text=True,
        ).stdout
    assert "dashboard/index.html" in tracked
    assert f".github/workflows/{PAGES_WORKFLOW_FILENAME}" in tracked


def test_static_export_falls_back_to_ledger_without_user_git(tmp_path: Path) -> None:
    import shutil
    import subprocess

    import pytest

    from archon_horizon.vcs.git import WorkspaceGit, git_available

    if not git_available():
        pytest.skip("git not installed")

    ws = _workspace(tmp_path)
    # Simulate a ledger-only workspace (no publish remote yet).
    if (ws / ".git").exists():
        shutil.rmtree(ws / ".git")
    main(["--root", str(ws), "dashboard", "--static", "--out", "dashboard", "--workflow"])

    git = WorkspaceGit(ws)
    assert git.is_repo()
    tracked = subprocess.run(
        ["git", "--git-dir", str(git.git_dir), "--work-tree", str(ws),
         "ls-files", "dashboard", ".github"],
        capture_output=True, text=True,
    ).stdout
    assert "dashboard/index.html" in tracked
    assert f".github/workflows/{PAGES_WORKFLOW_FILENAME}" in tracked
