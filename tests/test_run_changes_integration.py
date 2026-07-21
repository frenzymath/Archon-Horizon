"""End-to-end: the service's per-session change view over a real ledger.

Drives the pieces the orchestrator wires together — a numbered run/session, a
workspace ledger commit, and the integration event that maps the session to its
commit — then asserts `WorkspaceService.run_changes` (and its endpoint) reports
the deterministic diff + sorry delta the Logs view renders.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from archon_horizon.cli import main
from archon_horizon.core.events import Event
from archon_horizon.core.clock import utc_now
from archon_horizon.runlog import RunLogTree
from archon_horizon.server.service import WorkspaceService
from archon_horizon.transcript.model import TranscriptEvent, TranscriptKind
from archon_horizon.transcript.sink import JsonlTranscriptSink
from archon_horizon.vcs.git import WorkspaceGit, git_available
from archon_horizon.vcs.integration import integrate_workspace_baseline, integrate_workspace_session

pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")


def _ledger_commit(ws: Path, message: str, *files: str) -> None:
    """Agent-style commit into the workspace ledger with plain git (the raw-git
    path that replaced `horizon commit`). Provenance trailers are stamped by the
    ledger's prepare-commit-msg hook from the ARCHON_HORIZON_* env the test sets."""
    gd = str(WorkspaceGit(ws).git_dir)
    base = ["git", f"--git-dir={gd}", f"--work-tree={ws}"]
    for f in files:
        subprocess.run([*base, "add", f], cwd=str(ws), check=True)
    subprocess.run([*base, "commit", "-m", message], cwd=str(ws), check=True)


def _identity() -> None:
    for k, v in {
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@e",
    }.items():
        os.environ.setdefault(k, v)


def _emit_integration(service: WorkspaceService, integ, round_index: int) -> None:
    service.stores.events.append(Event(
        type="workspace.session.integrated",
        id=uuid.uuid4().hex,
        actor="orchestrator",
        data={
            "run_id": integ.run_id,
            "session": integ.session,
            "role": integ.role,
            "projects": list(integ.projects),
            "workspace_commit": integ.workspace_commit,
            "files": list(integ.workspace_files),
        },
    ))


def _emit_baseline(service: WorkspaceService, run_id: str, outcome, projects: tuple[str, ...]) -> None:
    service.stores.events.append(Event(
        type="workspace.run_baseline",
        id=uuid.uuid4().hex,
        actor="orchestrator",
        data={
            "run_id": run_id,
            "sha": outcome.sha,
            "changed": outcome.changed,
            "files": list(outcome.files),
            "projects": list(projects),
        },
    ))


def test_session_commits_view_reports_per_commit_change(tmp_path: Path, monkeypatch) -> None:
    # The commit-granular view: each commit a session made is reported on its own,
    # with only what THAT commit changed (message + per-file diff) — the basis for
    # the Logs "progress" cards.
    _identity()
    ws = tmp_path / "ws"
    main(["--root", str(ws), "init", "--no-interactive"])
    main(["--root", str(ws), "project", "add", "proj", "projects/proj"])

    service = WorkspaceService(ws)
    lean = ws / "projects" / "proj" / "Foo.lean"
    lean.parent.mkdir(parents=True, exist_ok=True)

    runs = RunLogTree(ws / ".archon-horizon" / "runs")
    run = runs.allocate()
    monkeypatch.setenv("ARCHON_HORIZON_AGENT_ROLE", "horizon")
    monkeypatch.setenv("ARCHON_HORIZON_ROOT", str(ws.resolve()))
    monkeypatch.setenv("ARCHON_HORIZON_RUN", run.id)
    monkeypatch.setenv("ARCHON_HORIZON_TASK", "T")
    monkeypatch.setenv("ARCHON_HORIZON_PROJECTS", "proj")

    # One session, TWO commits: introduce a sorry, then discharge it.
    s1 = run.new_session("horizon-T")
    monkeypatch.setenv("ARCHON_HORIZON_SESSION", s1.name)
    lean.write_text("theorem a : True := by sorry\n", "utf-8")
    _ledger_commit(ws, "Introduce a", str(lean))
    lean.write_text("theorem a : True := trivial\n", "utf-8")
    _ledger_commit(ws, "Discharge a", str(lean))
    i1 = integrate_workspace_session(service.workspace, run_id=run.id, session=s1.name,
                                     role="horizon", round_index=0, project="proj", projects=("proj",))
    _emit_integration(service, i1, 0)

    view = service.session_commits_view(run.id, s1.name)
    by_subject = {c["subject"]: c for c in view["commits"]}
    assert {"Introduce a", "Discharge a"} <= set(by_subject)
    # Each commit reports ONLY its own change (not a session-wide sum).
    assert by_subject["Introduce a"]["sorry_delta"] == 1
    assert by_subject["Discharge a"]["sorry_delta"] == -1
    # The two semantic commits are tagged as agent work…
    assert by_subject["Introduce a"]["kind"] == "agent"
    assert by_subject["Discharge a"]["kind"] == "agent"
    # …and the deterministic integration commit is present too, distinctly tagged
    # (so the UI can render agent progress prominently and bookkeeping subtly).
    assert any(c["kind"] == "integration" for c in view["commits"])

    # Endpoint parity + registered for static export.
    via = service.serve_endpoint(f"/api/session/commits?run={run.id}&session={s1.name}")
    assert via["commits"] == view["commits"]
    assert f"/api/session/commits?run={run.id}&session={s1.name}" in service.endpoints()
    assert all(row.get("created_at") for row in view["commits"])
    page = service.serve_endpoint(
        f"/api/session/commits?run={run.id}&session={s1.name}&offset=1&limit=1"
    )
    assert page["total"] == len(view["commits"])
    assert page["offset"] == 1 and len(page["commits"]) == 1
    assert page["has_more"] is True
    assert (
        f"/api/session/commits?run={run.id}&session={s1.name}&offset=1&limit=1"
        in service.endpoints()
    )

    # A single commit's file diff resolves via the &sha= param.
    disc = by_subject["Discharge a"]
    ep = f"/api/session/file-diff?run={run.id}&session={s1.name}&path=projects/proj/Foo.lean&sha={disc['sha']}"
    diff = service.serve_endpoint(ep)
    assert diff["available"] and "trivial" in diff["diff"]
    assert ep in service.endpoints()


def test_legacy_interactive_session_recovers_explicit_transcript_commit(
    tmp_path: Path, monkeypatch
) -> None:
    _identity()
    ws = tmp_path / "ws"
    main(["--root", str(ws), "init", "--no-interactive"])
    main(["--root", str(ws), "project", "add", "proj", "projects/proj"])
    for key in (
        "ARCHON_HORIZON_RUN",
        "ARCHON_HORIZON_SESSION",
        "ARCHON_HORIZON_AGENT_ROLE",
    ):
        monkeypatch.delenv(key, raising=False)

    runs = RunLogTree(ws / ".archon-horizon" / "runs")
    run = runs.allocate()
    session = run.new_session("horizon-interactive")
    started = utc_now() - timedelta(minutes=1)
    lean = ws / "projects" / "proj" / "Legacy.lean"
    lean.parent.mkdir(parents=True, exist_ok=True)
    lean.write_text("theorem legacy : True := by trivial\n", "utf-8")
    git = WorkspaceGit(ws)
    git.init()
    sha = git.commit("Legacy interactive proof", paths=["projects/proj/Legacy.lean"])
    assert sha
    session.write_meta({
        "role": "horizon",
        "interactive": True,
        "started_at": started.isoformat(),
        "ended_at": (utc_now() + timedelta(minutes=1)).isoformat(),
        "status": "ok",
    })
    JsonlTranscriptSink(session.transcript_path).emit(TranscriptEvent(
        TranscriptKind.TEXT,
        text=f"## Progress\n\nCompleted and committed as `{sha[:10]}`.",
    ))
    (session.path / "report.md").write_text(
        "# Interactive horizon session\n\nA generic placeholder.\n", "utf-8"
    )

    service = WorkspaceService(ws)
    view = service.session_commits_view(run.id, session.name)
    assert [(row["sha"], row["subject"]) for row in view["commits"]] == [
        (sha, "Legacy interactive proof")
    ]
    ref = session.transcript_path.relative_to(ws).as_posix()
    assert service.report(ref)["markdown"].startswith("## Progress")
