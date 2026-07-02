"""`horizon commit` — agent-authored semantic commits and their change view."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from archon_horizon.cli import main
from archon_horizon.core.events import Event
from archon_horizon.runlog import RunLogTree
from archon_horizon.server.service import WorkspaceService
from archon_horizon.vcs.git import WorkspaceGit, git_available
from archon_horizon.vcs.integration import integrate_workspace_session

pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")


def _identity() -> None:
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}.items():
        os.environ.setdefault(k, v)


def _agent_env(monkeypatch, ws: Path, session: str) -> None:
    monkeypatch.setenv("ARCHON_HORIZON_AGENT_ROLE", "horizon")
    monkeypatch.setenv("ARCHON_HORIZON_ROOT", str(ws.resolve()))
    monkeypatch.setenv("ARCHON_HORIZON_RUN", "0001")
    monkeypatch.setenv("ARCHON_HORIZON_SESSION", session)
    monkeypatch.setenv("ARCHON_HORIZON_TASK", "T1")
    monkeypatch.setenv("ARCHON_HORIZON_PROJECTS", "proj")


def test_horizon_commit_stamps_provenance_and_attributes_role(tmp_path: Path, monkeypatch) -> None:
    _identity()
    ws = tmp_path / "ws"
    assert main(["--root", str(ws), "init", "--no-interactive"]) == 0
    assert main(["--root", str(ws), "project", "add", "proj", "projects/proj"]) == 0
    lean = ws / "projects" / "proj" / "Foo.lean"
    lean.parent.mkdir(parents=True, exist_ok=True)
    lean.write_text("theorem a : True := trivial\n", "utf-8")

    _agent_env(monkeypatch, ws, "0002-horizon-T1")
    # No --root: resolves from ARCHON_HORIZON_ROOT (agent runs from a subdir).
    assert main(["commit", "-m", "Prove a via trivial", str(lean)]) == 0

    git = WorkspaceGit(ws)
    commits = git.session_commits("0001", "0002-horizon-T1")
    assert len(commits) == 1
    sha, subject = commits[0]
    assert subject == "Prove a via trivial"
    gd = ws / ".archon-horizon" / "vcs" / "workspace.git"
    import subprocess
    author = subprocess.run(["git", "--git-dir", str(gd), "show", "-s", "--format=%an", sha],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert author == "Archon Horizon (Horizon)"
    run_tr = subprocess.run(["git", "--git-dir", str(gd), "show", "-s",
                             "--format=%(trailers:key=Archon-Run,valueonly)", sha],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert run_tr == "0001"


def test_run_changes_aggregates_agent_commits(tmp_path: Path, monkeypatch) -> None:
    _identity()
    ws = tmp_path / "ws"
    assert main(["--root", str(ws), "init", "--no-interactive"]) == 0
    assert main(["--root", str(ws), "project", "add", "proj", "projects/proj"]) == 0
    proj = ws / "projects" / "proj"
    (proj).mkdir(parents=True, exist_ok=True)

    svc = WorkspaceService(ws)
    runs = RunLogTree(ws / ".archon-horizon" / "runs")
    run = runs.allocate()
    session = run.new_session("horizon-T1")
    _agent_env(monkeypatch, ws, session.name)

    # Two semantic agent commits during the session.
    (proj / "Foo.lean").write_text("theorem a : True := by sorry\n", "utf-8")
    assert main(["commit", "-m", "Add Foo skeleton", str(proj / "Foo.lean")]) == 0
    (proj / "Foo.lean").write_text("theorem a : True := trivial\ndef b := 0\n", "utf-8")
    assert main(["commit", "-m", "Discharge sorry, add b", str(proj / "Foo.lean")]) == 0

    # Orchestrator's shared-state integration commit for the same session.
    integ = integrate_workspace_session(svc.workspace, run_id=run.id, session=session.name,
                                        role="horizon", round_index=0, project="proj", projects=("proj",))
    svc.stores.events.append(Event(type="workspace.session.integrated", id=uuid.uuid4().hex,
        actor="orchestrator", data={"run_id": run.id, "session": session.name, "role": "horizon",
        "projects": ["proj"], "workspace_commit": integ.workspace_commit, "files": []}))

    changes = svc.run_changes(run.id)
    sess = next(s for s in changes["sessions"] if s["session"] == session.name)
    # Aggregated over the session's commits (both agent commits + integration).
    assert sess["base_source"] == "session-commits"
    assert len(sess["commits"]) >= 2
    assert {c["subject"] for c in sess["commits"]} >= {"Add Foo skeleton", "Discharge sorry, add b"}
    foo = next(r for r in sess["files"] if r["path"].endswith("Foo.lean"))
    # Net over the session: sorry went 0→1→0 = 0 net; ended at 0 with a new def.
    assert foo["sorry_after"] == 0
    assert foo["decl_delta"].get("def") == 1
