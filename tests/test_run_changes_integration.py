"""End-to-end: the service's per-session change view over a real ledger.

Drives the pieces the orchestrator wires together — a numbered run/session, a
workspace ledger commit, and the integration event that maps the session to its
commit — then asserts `WorkspaceService.run_changes` (and its endpoint) reports
the deterministic diff + sorry delta the Logs view renders.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from archon_horizon.cli import main
from archon_horizon.core.events import Event
from archon_horizon.runlog import RunLogTree
from archon_horizon.server.service import WorkspaceService
from archon_horizon.vcs.git import git_available
from archon_horizon.vcs.integration import integrate_workspace_session

pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")


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


def test_run_changes_reports_per_session_sorry_delta(tmp_path: Path) -> None:
    _identity()
    ws = tmp_path / "ws"
    main(["--root", str(ws), "init", "--no-interactive"])
    main(["--root", str(ws), "project", "add", "proj", "projects/proj"])

    service = WorkspaceService(ws)
    lean = ws / "projects" / "proj" / "Foo.lean"
    lean.parent.mkdir(parents=True, exist_ok=True)

    runs = RunLogTree(ws / ".archon-horizon" / "runs")
    run = runs.allocate()

    # Session 1: two sorries introduced.
    s1 = run.new_session("horizon-T")
    lean.write_text("theorem a : True := by sorry\ntheorem b : True := by sorry\n", "utf-8")
    i1 = integrate_workspace_session(service.workspace, run_id=run.id, session=s1.name,
                                     role="horizon", round_index=0, project="proj", projects=("proj",))
    _emit_integration(service, i1, 0)

    # Session 2: one sorry discharged.
    s2 = run.new_session("horizon-T")
    lean.write_text("theorem a : True := trivial\ntheorem b : True := by sorry\n", "utf-8")
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
