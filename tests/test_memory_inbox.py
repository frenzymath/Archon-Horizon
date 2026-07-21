"""Memory-as-inbox: durable notes are `memory` inbox items, rendered as memory."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.cli import main

_CONFIG = """
workspace:
  name: w
  rounds: 1
  ground_agent: {harness: inf, subagents: []}
  horizon_agent: {harness: hor}
harnesses:
  inf: {kind: "null"}
  hor: {kind: "null"}
projects:
  ag-main: {path: projects/ag-main}
"""


def test_memory_item_pulled_via_cli_kind_filter(tmp_path: Path, capsys) -> None:
    # Memory lives in the inbox; the agent pulls durable notes with
    # `horizon inbox list --kind memory --json` rather than having them pushed.
    import json

    ws = tmp_path / "ws"
    (ws / "projects" / "ag-main").mkdir(parents=True)
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")

    note = "induction route fails\n\ntried induction on n, fails on the succ case"
    main(["--root", str(ws), "inbox", "add", "--kind", "memory", "--to", "horizon",
          "--author", "horizon", "--body", note])
    main(["--root", str(ws), "inbox", "add", "--body", "plain hint\n\nnot memory"])
    capsys.readouterr()

    assert main(["--root", str(ws), "inbox", "list", "--kind", "memory", "--json"]) == 0
    items = json.loads(capsys.readouterr().out)["items"]
    assert [i["kind"] for i in items] == ["memory"]
    assert note in items[0]["body"]
