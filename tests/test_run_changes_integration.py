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
from pathlib import Path

import pytest

from archon_horizon.cli import main
from archon_horizon.core.events import Event
from archon_horizon.runlog import RunLogTree
from archon_horizon.server.service import WorkspaceService
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


def test_run_changes_reports_per_session_sorry_delta(tmp_path: Path, monkeypatch) -> None:
    _identity()
    ws = tmp_path / "ws"
    main(["--root", str(ws), "init", "--no-interactive"])
    main(["--root", str(ws), "project", "add", "proj", "projects/proj"])

    service = WorkspaceService(ws)
    lean = ws / "projects" / "proj" / "Foo.lean"
    lean.parent.mkdir(parents=True, exist_ok=True)

    runs = RunLogTree(ws / ".archon-horizon" / "runs")
    run = runs.allocate()
    # Agent sessions are attributed from their own semantic commits, so each
    # session records its change with an agent-authored `horizon commit` (the
    # integration sweep is deterministic bookkeeping and must not feed the diff).
    monkeypatch.setenv("ARCHON_HORIZON_AGENT_ROLE", "horizon")
    monkeypatch.setenv("ARCHON_HORIZON_ROOT", str(ws.resolve()))
    monkeypatch.setenv("ARCHON_HORIZON_RUN", run.id)
    monkeypatch.setenv("ARCHON_HORIZON_TASK", "T")
    monkeypatch.setenv("ARCHON_HORIZON_PROJECTS", "proj")

    # Session 1: two sorries introduced.
    s1 = run.new_session("horizon-T")
    monkeypatch.setenv("ARCHON_HORIZON_SESSION", s1.name)
    lean.write_text("theorem a : True := by sorry\ntheorem b : True := by sorry\n", "utf-8")
    _ledger_commit(ws, "Introduce a and b", str(lean))
    i1 = integrate_workspace_session(service.workspace, run_id=run.id, session=s1.name,
                                     role="horizon", round_index=0, project="proj", projects=("proj",))
    _emit_integration(service, i1, 0)

    # Session 2: one sorry discharged.
    s2 = run.new_session("horizon-T")
    monkeypatch.setenv("ARCHON_HORIZON_SESSION", s2.name)
    lean.write_text("theorem a : True := trivial\ntheorem b : True := by sorry\n", "utf-8")
    _ledger_commit(ws, "Discharge a", str(lean))
    i2 = integrate_workspace_session(service.workspace, run_id=run.id, session=s2.name,
                                     role="horizon", round_index=1, project="proj", projects=("proj",))
    _emit_integration(service, i2, 1)

    changes = service.run_changes(run.id)
    assert [s["session"] for s in changes["sessions"]] == [s1.name, s2.name]
    assert changes["sessions"][0]["sorry_delta"] == 2   # introduced two
    assert changes["sessions"][1]["sorry_delta"] == -1  # discharged one
    # Cumulative trend: +2 then +1 net.
    assert [t["cumulative_sorry_delta"] for t in changes["trend"]] == [2, 1]

    # Per-file trend: Foo.lean appears at both sessions with its sorry-after.
    assert [p["sorry_after"] for p in changes["file_trends"]["projects/proj/Foo.lean"]] == [2, 1]

    # The endpoint the dashboard (and static export) calls returns the same thing.
    via_endpoint = service.serve_endpoint(f"/api/run/changes?run={run.id}")
    assert via_endpoint["trend"] == changes["trend"]
    # Both change and per-file-diff endpoints are enumerated for static precompute.
    eps = service.endpoints()
    assert f"/api/run/changes?run={run.id}" in eps
    file_diff_ep = f"/api/session/file-diff?run={run.id}&session={s2.name}&path=projects/proj/Foo.lean"
    assert file_diff_ep in eps
    # And the file diff resolves to a real unified diff of the change.
    diff = service.serve_endpoint(file_diff_ep)
    assert diff["available"] and "trivial" in diff["diff"] and "Foo.lean" in diff["diff"]


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

    # A single commit's file diff resolves via the &sha= param.
    disc = by_subject["Discharge a"]
    ep = f"/api/session/file-diff?run={run.id}&session={s1.name}&path=projects/proj/Foo.lean&sha={disc['sha']}"
    diff = service.serve_endpoint(ep)
    assert diff["available"] and "trivial" in diff["diff"]
    assert ep in service.endpoints()


def test_working_changes_use_run_baseline_and_session_dirty_snapshot(tmp_path: Path) -> None:
    _identity()
    ws = tmp_path / "ws"
    main(["--root", str(ws), "init", "--no-interactive"])
    main(["--root", str(ws), "project", "add", "proj", "projects/proj"])

    service = WorkspaceService(ws)
    proj = ws / "projects" / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    foo = proj / "Foo.lean"
    stale = proj / "Stale.lean"
    bar = proj / "Bar.lean"
    foo.write_text("theorem a : True := by sorry\n", "utf-8")

    runs = RunLogTree(ws / ".archon-horizon" / "runs")
    run = runs.allocate()
    baseline = integrate_workspace_baseline(service.workspace, run_id=run.id, projects=("proj",))
    _emit_baseline(service, run.id, baseline, ("proj",))

    stale.write_text("theorem stale : True := by sorry\n", "utf-8")
    session = run.new_session("horizon-T")
    session.write_meta({
        "role": "horizon",
        "status": "running",
        "projects": ["proj"],
        "dirty_at_start": ["projects/proj/Stale.lean"],
    })

    foo.write_text("theorem a : True := trivial\n", "utf-8")
    bar.write_text("theorem b : True := by sorry\n", "utf-8")

    changes = service.working_changes(run.id, session.name)
    paths = {row["path"] for row in changes["files"]}
    assert paths == {"projects/proj/Foo.lean", "projects/proj/Bar.lean"}
    assert changes["base"] == baseline.sha
    assert changes["excluded_count"] == 1
    assert next(row for row in changes["files"] if row["path"].endswith("Foo.lean"))["sorry_delta"] == -1

    state_run = next(r for r in service.state()["runs"] if r["id"] == run.id)
    assert state_run["sessions"][0]["session"] == "0000-system-baseline"
    assert state_run["sessions"][0]["meta"]["role"] == "system"


def test_working_changes_excludes_a_parallel_runs_files_in_the_same_project(
    tmp_path: Path, monkeypatch
) -> None:
    """The live view is scoped to files THIS run's own agent commits touched, so a
    parallel run's uncommitted changes to a sibling file in the shared worktree
    (same project) do not leak in — the reported "chapter 0 shouldn't be here" bug."""
    _identity()
    ws = tmp_path / "ws"
    main(["--root", str(ws), "init", "--no-interactive"])
    main(["--root", str(ws), "project", "add", "proj", "projects/proj"])

    service = WorkspaceService(ws)
    proj = ws / "projects" / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    mine = proj / "Mine.lean"
    sibling = proj / "Sibling.lean"

    runs = RunLogTree(ws / ".archon-horizon" / "runs")
    run_a = runs.allocate()   # "my" run — works on Mine.lean
    run_b = runs.allocate()   # a parallel run — works on Sibling.lean

    monkeypatch.setenv("ARCHON_HORIZON_AGENT_ROLE", "horizon")
    monkeypatch.setenv("ARCHON_HORIZON_ROOT", str(ws.resolve()))
    monkeypatch.setenv("ARCHON_HORIZON_TASK", "T")
    monkeypatch.setenv("ARCHON_HORIZON_PROJECTS", "proj")

    # Run A commits Mine.lean; run B commits Sibling.lean (both agent commits).
    sa = run_a.new_session("horizon-A")
    monkeypatch.setenv("ARCHON_HORIZON_RUN", run_a.id)
    monkeypatch.setenv("ARCHON_HORIZON_SESSION", sa.name)
    mine.write_text("theorem m : True := by sorry\n", "utf-8")
    _ledger_commit(ws, "run A: add Mine", str(mine))

    sb = run_b.new_session("horizon-B")
    monkeypatch.setenv("ARCHON_HORIZON_RUN", run_b.id)
    monkeypatch.setenv("ARCHON_HORIZON_SESSION", sb.name)
    sibling.write_text("theorem s : True := by sorry\n", "utf-8")
    _ledger_commit(ws, "run B: add Sibling", str(sibling))

    # Run A's live session, with fresh UNCOMMITTED edits to both files in the
    # shared worktree — Mine.lean is A's own work; Sibling.lean is B's leftover.
    live = run_a.new_session("horizon-A")
    live.write_meta({"role": "horizon", "status": "running", "projects": ["proj"]})
    mine.write_text("theorem m : True := trivial\n", "utf-8")
    sibling.write_text("theorem s : True := trivial\n", "utf-8")

    changes = service.working_changes(run_a.id, live.name)
    paths = {row["path"] for row in changes["files"]}
    assert paths == {"projects/proj/Mine.lean"}          # Sibling.lean (run B's) excluded
    assert changes["scope_source"] == "run-agent-files"
