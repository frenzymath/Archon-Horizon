"""The vendored semantic graph is exposed through ``horizon graph``."""

from __future__ import annotations

import json
from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.hgraph import Graph


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "workspace"
    project = root / "projects" / "demo"
    project.mkdir(parents=True)
    (root / "config.yaml").write_text(
        "workspace: {name: test}\nprojects:\n  demo: {path: projects/demo}\n",
        "utf-8",
    )
    return root, project


def test_horizon_graph_forwards_to_vendored_cli(tmp_path: Path, capsys) -> None:
    root, project = _workspace(tmp_path)
    graph = Graph.open(project)
    graph.add_node("A theorem", type="tex", key="a", lean_status="empty")

    assert main(["--root", str(root), "graph", "--project", "demo", "stats", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["nodes"] == 1
    assert payload["by_type"] == {"tex": 1}


def test_horizon_graph_uses_the_only_project_by_default(tmp_path: Path, capsys) -> None:
    root, project = _workspace(tmp_path)
    Graph.open(project).add_node("A theorem", type="tex", key="a")

    assert main(["--root", str(root), "graph", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [node["title"] for node in payload] == ["A theorem"]


def test_agent_graph_comment_inherits_authorship_and_provenance(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root, project = _workspace(tmp_path)
    graph = Graph.open(project)
    node_id = graph.add_node("A theorem", type="tex", key="a")
    monkeypatch.setenv("ARCHON_HORIZON_AGENT_ROLE", "horizon")
    monkeypatch.setenv("ARCHON_HORIZON_RUN", "0004")
    monkeypatch.setenv("ARCHON_HORIZON_SESSION", "0002-horizon")

    assert main([
        "--root", str(root), "graph", "add", "comment", "key:a", "--content", "Use the compactness route."
    ]) == 0

    comment = Graph.open(project).comments(node_id)[0]
    assert comment.meta["author"] == "horizon"
    assert comment.meta["provenance"] == {
        "run": "0004", "session": "0002-horizon", "role": "horizon"
    }
