"""Per-session change summaries: deterministic diff + sorry delta from the ledger."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from archon_horizon.core.workspace import Project, Workspace
from archon_horizon.server.changes_api import session_change_summary
from archon_horizon.vcs.git import git_available
from archon_horizon.vcs.integration import integrate_workspace_session

pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")


def _identity() -> None:
    os.environ.setdefault("GIT_AUTHOR_NAME", "test")
    os.environ.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    os.environ.setdefault("GIT_COMMITTER_NAME", "test")
    os.environ.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")


def _workspace(tmp_path: Path) -> Workspace:
    (tmp_path / "config.yaml").write_text("workspace: {name: ws}\n", "utf-8")
    return Workspace(name="ws", root=tmp_path, projects={"p": Project(name="p", path=Path("projects/p"))})


def test_session_change_summary_reports_sorry_delta(tmp_path: Path) -> None:
    _identity()
    ws = _workspace(tmp_path)
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    lean = proj / "Foo.lean"

    # Session 1: introduce a file with two sorries.
    lean.write_text("theorem a : True := by sorry\ntheorem b : True := by sorry\n", "utf-8")
    s1 = integrate_workspace_session(ws, run_id="0001", session="0001-horizon", role="horizon",
                                     round_index=0, project="p", task_id="T", projects=("p",))

    # Session 2: discharge one sorry.
    lean.write_text("theorem a : True := trivial\ntheorem b : True := by sorry\n", "utf-8")
    s2 = integrate_workspace_session(ws, run_id="0001", session="0002-horizon", role="horizon",
                                     round_index=1, project="p", task_id="T", projects=("p",))

    projpath = (proj.relative_to(tmp_path)).as_posix()
    # Diff session 2 against session 1's commit (the previous session), as the
    # service does — not the raw git parent.
    summary = session_change_summary(tmp_path, s2.workspace_commit, (projpath,), base=s1.workspace_commit)

    assert summary["available"] is True
    assert summary["sorry_delta"] == -1
    assert summary["lean"]["sorry_delta"] == -1
    row = next(r for r in summary["files"] if r["path"].endswith("Foo.lean"))
    assert row["category"] == "lean"
    assert row["sorry_before"] == 2 and row["sorry_after"] == 1 and row["sorry_delta"] == -1
    assert row["loc_before"] == 2 and row["loc_after"] == 2  # two theorem lines
    assert row["decl_after"] == {"theorem": 2}
    assert summary["lean"]["decl_after"] == {"theorem": 2}
    # The run's first session (base=None) sees the file as newly added, 2 sorries.
    first = session_change_summary(tmp_path, s1.workspace_commit, (projpath,), base=None)
    assert first["available"] and first["sorry_delta"] == 2
    assert next(r for r in first["files"] if r["path"].endswith("Foo.lean"))["added"] is True
    assert next(r for r in first["files"] if r["path"].endswith("Foo.lean"))["added"] is True


def test_session_change_summary_reports_declaration_deltas(tmp_path: Path) -> None:
    _identity()
    ws = _workspace(tmp_path)
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    lean = proj / "Foo.lean"
    tex = proj / "blueprint" / "chapter.tex"
    tex.parent.mkdir()
    projpath = proj.relative_to(tmp_path).as_posix()

    lean.write_text("def f : Nat := 0\ntheorem a : True := trivial\n", "utf-8")
    tex.write_text("\\begin{theorem}\\label{a} A.\\end{theorem}\n", "utf-8")
    s1 = integrate_workspace_session(ws, run_id="0001", session="0001-horizon", role="horizon",
                                     round_index=0, project="p", projects=("p",))

    lean.write_text(
        "def f : Nat := 0\n"
        "theorem a : True := trivial\n"
        "lemma b : True := trivial\n",
        "utf-8",
    )
    tex.write_text(
        "\\begin{theorem}\\label{a} A.\\end{theorem}\n"
        "\\begin{lemma}\\label{b} B.\\end{lemma}\n"
        "\\begin{definition}\\label{c} C.\\end{definition}\n",
        "utf-8",
    )
    s2 = integrate_workspace_session(ws, run_id="0001", session="0002-horizon", role="horizon",
                                     round_index=1, project="p", projects=("p",))

    summary = session_change_summary(tmp_path, s2.workspace_commit, (projpath,), base=s1.workspace_commit)
    lean_row = next(r for r in summary["files"] if r["path"].endswith("Foo.lean"))
    tex_row = next(r for r in summary["files"] if r["path"].endswith("chapter.tex"))

    assert lean_row["decl_delta"] == {"lemma": 1}
    assert summary["lean"]["decl_delta"] == {"lemma": 1}
    assert tex_row["decl_delta"] == {"definition": 1, "lemma": 1}
    assert summary["blueprint"]["decl_delta"] == {"definition": 1, "lemma": 1}


def test_first_session_falls_back_to_git_parent(tmp_path: Path) -> None:
    # With a run-start system commit, the first agent session has no explicit
    # previous-session base, but its git parent (the system snapshot) is used —
    # so it shows a real diff, not "new file" on everything.
    _identity()
    ws = _workspace(tmp_path)
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    lean = proj / "Foo.lean"
    projpath = proj.relative_to(tmp_path).as_posix()

    # System snapshot at run start (2 sorries), then the first agent session (1).
    lean.write_text("theorem a : True := by sorry\ntheorem b : True := by sorry\n", "utf-8")
    integrate_workspace_session(ws, run_id="0001", session="0001-system", role="system",
                                round_index=0, projects=("p",))
    lean.write_text("theorem a : True := trivial\ntheorem b : True := by sorry\n", "utf-8")
    s2 = integrate_workspace_session(ws, run_id="0001", session="0002-horizon", role="horizon",
                                     round_index=0, project="p", projects=("p",))

    # base=None → falls back to the parent (the system snapshot): −1 sorry, not initial.
    summary = session_change_summary(tmp_path, s2.workspace_commit, (projpath,), base=None)
    assert summary["available"] and summary["initial"] is False
    assert summary["sorry_delta"] == -1


def test_worktree_summary_shows_uncommitted_changes(tmp_path: Path) -> None:
    # A running session hasn't committed: the live view diffs the working tree
    # against the last committed session, including brand-new untracked files.
    _identity()
    ws = _workspace(tmp_path)
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    lean = proj / "Foo.lean"
    projpath = proj.relative_to(tmp_path).as_posix()

    lean.write_text("theorem a : True := by sorry\n", "utf-8")
    s1 = integrate_workspace_session(ws, run_id="0001", session="0001-horizon", role="horizon",
                                     round_index=0, project="p", projects=("p",))

    # Now (uncommitted) discharge the sorry and add a new file.
    lean.write_text("theorem a : True := trivial\n", "utf-8")
    (proj / "Bar.lean").write_text("theorem b : True := by sorry\n", "utf-8")

    summary = session_change_summary(tmp_path, None, (projpath,), base=s1.workspace_commit, worktree=True)
    assert summary["available"] and summary["worktree"] is True
    assert summary["base_source"] == "working-tree"
    paths = {r["path"]: r for r in summary["files"]}
    assert paths[f"{projpath}/Foo.lean"]["sorry_delta"] == -1
    # The untracked new file is included as an addition with its sorry.
    assert paths[f"{projpath}/Bar.lean"]["added"] is True
    assert paths[f"{projpath}/Bar.lean"]["sorry_after"] == 1


def test_session_change_summary_degrades_without_git(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    # No sha → unavailable, but still lists fallback files (from the event).
    summary = session_change_summary(tmp_path, None, ("projects/p",), fallback_files=("projects/p/Foo.lean",))
    assert summary["available"] is False
    assert summary["files"] == [{"path": "projects/p/Foo.lean", "category": "lean"}]


def test_diffs_against_git_parent_not_far_same_run_ancestor(tmp_path: Path) -> None:
    # The shared ledger interleaves runs: a session's commit parent may belong to
    # another run. The change summary (base=None) must diff against that git
    # parent — what this commit actually changed — not against a far same-run
    # ancestor, which would report every interleaved file.
    _identity()
    ws = _workspace(tmp_path)
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    projpath = proj.relative_to(tmp_path).as_posix()

    # Commit 1 (run A): Foo exists.
    (proj / "Foo.lean").write_text("theorem a : True := by sorry\n", "utf-8")
    integrate_workspace_session(ws, run_id="A", session="0001-horizon", role="horizon",
                                round_index=0, project="p", projects=("p",))
    # Commit 2 (another run B) lands in between, adding many unrelated files.
    for i in range(5):
        (proj / f"Other{i}.lean").write_text("def x := 0\n" * 50, "utf-8")
    integrate_workspace_session(ws, run_id="B", session="0001-horizon", role="horizon",
                                round_index=0, project="p", projects=("p",))
    # Commit 3 (run A again): only touches Foo. Its git parent is B's commit.
    (proj / "Foo.lean").write_text("theorem a : True := trivial\n", "utf-8")
    s3 = integrate_workspace_session(ws, run_id="A", session="0003-horizon", role="horizon",
                                     round_index=1, project="p", projects=("p",))

    summary = session_change_summary(tmp_path, s3.workspace_commit, (projpath,), base=None)
    changed = {r["path"] for r in summary["files"]}
    # Only Foo.lean — the 5 Other*.lean from run B are in the parent, not this diff.
    assert changed == {f"{projpath}/Foo.lean"}
    assert summary["sorry_delta"] == -1
