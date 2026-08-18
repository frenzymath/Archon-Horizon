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


def _ledger_commit(
    ws: Path, message: str, *files: str, summaries: tuple[str, ...] = ()
) -> None:
    """Agent-style commit into the workspace ledger with plain git (the raw-git
    path that replaced `horizon commit`). Provenance trailers are stamped by the
    ledger's prepare-commit-msg hook from the ARCHON_HORIZON_* env the test sets."""
    gd = str(WorkspaceGit(ws).git_dir)
    base = ["git", f"--git-dir={gd}", f"--work-tree={ws}"]
    for f in files:
        subprocess.run([*base, "add", f], cwd=str(ws), check=True)
    trailers = [arg for summary in summaries for arg in ("--trailer", f"Summary={summary}")]
    subprocess.run([*base, "commit", "-m", message, *trailers], cwd=str(ws), check=True)


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
    assert main([
        "--root", str(ws), "inbox", "add", "--kind", "issue",
        "--body", "Representability decision\n\nRecord the choice made in this session.",
    ]) == 0
    assert main([
        "--root", str(ws), "inbox", "comment", "I-0001",
        "--body", "The session kept the stronger branch open.",
    ]) == 0
    assert main([
        "--root", str(ws), "inbox", "unread", "I-0001",
    ]) == 0
    lean.write_text("theorem a : True := by sorry\n", "utf-8")
    _ledger_commit(
        ws,
        "Introduce a",
        str(lean),
        summaries=(
            "**Scaffold.** Added the temporary goal $a : \\mathrm{True}$.",
            "**Next.** Replace `sorry` with the canonical constructor.",
        ),
    )
    lean.write_text("theorem a : True := trivial\n", "utf-8")
    _ledger_commit(ws, "Discharge a", str(lean))
    i1 = integrate_workspace_session(service.workspace, run_id=run.id, session=s1.name,
                                     role="horizon", round_index=0, project="proj", projects=("proj",))
    _emit_integration(service, i1, 0)

    view = service.session_commits_view(run.id, s1.name)
    assert {key: value for key, value in view["inbox"].items() if key != "items"} == {
        "created": 1, "comments": 1, "actions": 1, "total": 1,
    }
    assert {key: value for key, value in view["inbox"]["items"][0].items()
            if key != "last_activity_at"} == {
        "id": "I-0001",
        "title": "Representability decision",
        "kind": "issue",
        "status": "open",
        "created": True,
        "comments": 1,
        "actions": 1,
    }
    assert view["inbox"]["items"][0]["last_activity_at"]
    by_subject = {c["subject"]: c for c in view["commits"]}
    assert {"Introduce a", "Discharge a"} <= set(by_subject)
    # Each commit reports ONLY its own change (not a session-wide sum).
    assert by_subject["Introduce a"]["sorry_delta"] == 1
    assert by_subject["Discharge a"]["sorry_delta"] == -1
    assert by_subject["Introduce a"]["summary"] == (
        "**Scaffold.** Added the temporary goal $a : \\mathrm{True}$.\n\n"
        "**Next.** Replace `sorry` with the canonical constructor."
    )
    assert by_subject["Discharge a"]["summary"] == ""
    # The two semantic commits are tagged as agent work…
    assert by_subject["Introduce a"]["kind"] == "agent"
    assert by_subject["Discharge a"]["kind"] == "agent"
    # …and the deterministic integration commit is present too, distinctly tagged
    # (so the UI can render agent progress prominently and bookkeeping subtly).
    assert any(c["kind"] == "integration" for c in view["commits"])
    assert view["commit_counts"]["agent"] == 2
    assert view["commit_counts"]["integration"] == 1
    assert view["outcome"]["durable_result"] == "agent_commit"

    # Endpoint parity + registered for static export.
    via = service.serve_endpoint(f"/api/session/commits?run={run.id}&session={s1.name}")
    assert via["commits"] == view["commits"]
    assert via["inbox"] == view["inbox"]
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
    first_cards = service.serve_endpoint(
        f"/api/session/commits?run={run.id}&session={s1.name}&offset=0&limit=3"
    )
    assert len(first_cards["commits"]) == min(3, len(view["commits"]))
    assert (
        f"/api/session/commits?run={run.id}&session={s1.name}&offset=0&limit=3"
        in service.endpoints()
    )

    # A single commit's file diff resolves via the &sha= param.
    disc = by_subject["Discharge a"]
    ep = f"/api/session/file-diff?run={run.id}&session={s1.name}&path=projects/proj/Foo.lean&sha={disc['sha']}"
    diff = service.serve_endpoint(ep)
    assert diff["available"] and "trivial" in diff["diff"]
    assert ep in service.endpoints()

    # Inbox-only work remains visible in Logs even when a session made no commit.
    s2 = run.new_session("horizon-inbox-only")
    monkeypatch.setenv("ARCHON_HORIZON_SESSION", s2.name)
    assert main([
        "--root", str(ws), "inbox", "add", "--kind", "info",
        "--body", "Session hand-off\n\nNo source change was needed.",
    ]) == 0
    inbox_only = service.session_commits_view(run.id, s2.name)
    assert inbox_only["commits"] == []
    assert inbox_only["outcome"]["durable_result"] == "no_commit"
    assert inbox_only["inbox"]["created"] == 1
    assert inbox_only["inbox"]["items"][0]["title"] == "Session hand-off"


def test_session_commits_view_reports_roadmap_activity(tmp_path: Path, monkeypatch) -> None:
    # The per-session "Roadmap activity" feed: items created, re-statused, or
    # commented on by THIS session, attributed by durable provenance (creation
    # provenance, provenance-stamped status history, and comment provenance).
    _identity()
    ws = tmp_path / "ws"
    main(["--root", str(ws), "init", "--no-interactive"])
    main(["--root", str(ws), "project", "add", "proj", "projects/proj"])

    service = WorkspaceService(ws)
    runs = RunLogTree(ws / ".archon-horizon" / "runs")
    run = runs.allocate()
    monkeypatch.setenv("ARCHON_HORIZON_AGENT_ROLE", "horizon")
    monkeypatch.setenv("ARCHON_HORIZON_ROOT", str(ws.resolve()))
    monkeypatch.setenv("ARCHON_HORIZON_RUN", run.id)
    monkeypatch.setenv("ARCHON_HORIZON_TASK", "T")
    monkeypatch.setenv("ARCHON_HORIZON_PROJECTS", "proj")

    session = run.new_session("horizon-roadmap")
    monkeypatch.setenv("ARCHON_HORIZON_SESSION", session.name)
    assert main([
        "--root", str(ws), "roadmap", "add", "--id", "A.1",
        "--title", "Prove the crux", "--project", "proj",
    ]) == 0
    assert main([
        "--root", str(ws), "roadmap", "set", "A.1", "--status", "active",
    ]) == 0
    assert main([
        "--root", str(ws), "roadmap", "comment", "A.1",
        "--body", "Started the active push on the crux.",
    ]) == 0

    view = service.session_commits_view(run.id, session.name)
    assert {key: value for key, value in view["roadmap"].items() if key != "items"} == {
        "created": 1, "status_changes": 1, "comments": 1, "total": 1,
    }
    row = view["roadmap"]["items"][0]
    assert row["id"] == "A.1"
    assert row["title"] == "Prove the crux"
    assert row["created"] is True
    assert row["status_changes"] == 1
    assert row["status_to"] == "active"
    assert row["comments"] == 1
    assert row["last_activity_at"]

    # A different session sees none of it (attribution is per-session, not mtime).
    other = run.new_session("horizon-other")
    monkeypatch.setenv("ARCHON_HORIZON_SESSION", other.name)
    assert main([
        "--root", str(ws), "roadmap", "set", "A.1", "--status", "done",
    ]) == 0
    first = service.session_commits_view(run.id, session.name)
    # The first session's status_changes count is unchanged by the other session.
    assert first["roadmap"]["items"][0]["status_changes"] == 1
    second = service.session_commits_view(run.id, other.name)
    assert second["roadmap"]["items"][0]["status_changes"] == 1
    assert second["roadmap"]["items"][0]["created"] is False


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
