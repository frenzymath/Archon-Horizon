"""The vendored hgraph collaboration command remains available through Horizon."""

from __future__ import annotations

import json
from pathlib import Path

from archon_horizon.hgraph import Graph
from archon_horizon.hgraph import collaboration
from archon_horizon.hgraph.cli import main


def test_review_send_dry_run_renders_pending_attachment(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    graph = Graph.open(tmp_path)
    node_id = graph.add_node(
        "Pythagoras", type="tex", id="pythagoras", label="thm:pythagoras",
        content_type="theorem", chapter="Geometry"
    )
    graph.add_attachment(
        node_id, "review", "The proof should cite the preceding lemma.",
        author="Ada", maths_verdict="good", lean_verdict="bad"
    )

    calls: list[list[str]] = []

    def fake_run(args: list[str], *, cwd: Path, input_text: str | None = None) -> str:
        calls.append(args)
        if args == ["git", "rev-parse", "--show-toplevel"]:
            return str(tmp_path)
        if args == ["git", "rev-parse", "HEAD"]:
            return "0123456789abcdef"
        if args[:2] == ["gh", "api"]:
            return json.dumps({"tree": [], "truncated": False})
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(collaboration.shutil, "which", lambda _command: "/usr/bin/gh")
    monkeypatch.setattr(collaboration, "_run", fake_run)

    assert main([
        "--root", str(tmp_path), "review", "send", "--dry-run", "--reviews-only",
        "--repo", "owner/repo", "--base", "main"
    ]) == 0
    output = capsys.readouterr().out

    assert "ISSUE BODY" in output
    assert "ISSUE COMMENT 1/1" in output
    assert "Review: Pythagoras" in output
    assert "**Verdict:** good" in output
    assert not any(call[:2] == ["gh", "issue"] for call in calls)
