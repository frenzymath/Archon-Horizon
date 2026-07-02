"""CLI smoke: the command surface works against a scaffolded workspace."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from archon_horizon.cli import main


def _run(root: Path, *argv: str) -> int:
    return main(["--root", str(root), *argv])


def _use_null_engines(ws: Path) -> None:
    """Swap the scaffolded claude/codex harnesses for the no-op engine so the
    test never shells out to a real CLI."""
    cfg = ws / "config.yaml"
    text = (
        cfg.read_text("utf-8")
        .replace('kind: "claude-code"', 'kind: "null"')
        .replace("kind: claude-code", 'kind: "null"')
        .replace('kind: "codex"', 'kind: "null"')
        .replace("kind: codex", 'kind: "null"')
    )
    cfg.write_text(text, "utf-8")


def test_full_cli_flow(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    assert _run(ws, "init", "--no-interactive") == 0
    assert (ws / "config.yaml").exists()
    assert (ws / ".archon-horizon" / "subagents").is_dir()
    _use_null_engines(ws)

    assert _run(ws, "project", "add", "ag-main", "projects/ag-main", "--build", "lake build") == 0
    assert (ws / "projects" / "ag-main").is_dir()

    assert _run(ws, "inbox", "add", "--kind", "hint", "--body", "affine first\n\nstart with the affine case") == 0
    assert _run(ws, "inbox", "list") == 0
    assert _run(ws, "inbox", "complete", "I-0001") == 0

    assert _run(ws, "dashboard", "--static") == 0
    assert (ws / "dashboard" / "index.html").exists()

    assert _run(ws, "sync") == 0

    # Engines are not installed; dry-run must still plan without crashing.
    assert _run(ws, "run", "ag-main", "--rounds", "1", "--dry-run") == 0
    assert (ws / ".archon-horizon" / "runs" / "0001" / "run.yaml").exists()
    assert _run(ws, "run", "--dry-run") == 1
    assert _run(ws, "run", ".", "--rounds", "1", "--dry-run") == 0
    assert (ws / ".archon-horizon" / "tasks" / "items" / "workspace-all.yaml").exists()


def test_roadmap_command_writes_safe_yaml(tmp_path: Path) -> None:
    import yaml

    ws = tmp_path / "ws"
    assert _run(ws, "init", "--no-interactive") == 0
    _use_null_engines(ws)
    assert _run(ws, "project", "add", "ag-main", "projects/ag-main", "--build", "lake build") == 0

    # The exact shape that corrupted a hand-edited roadmap: an apostrophe and a
    # bare colon inside the summary. Through the CLI it must round-trip cleanly.
    tricky = "Kleiman's map: build ker(Pic(k[e]) -> Pic(k)) iso H1"
    assert _run(
        ws, "roadmap", "add", "--id", "A.3", "--title", "tangent",
        "--project", "ag-main", "--summary", tricky, "--status", "active",
    ) == 0

    roadmap_file = ws / ".archon-horizon" / "roadmap" / "items" / "A.3.yaml"
    item = yaml.safe_load(roadmap_file.read_text("utf-8"))  # must be valid YAML
    assert item["id"] == "A.3" and item["summary"] == tricky and item["status"] == "active"

    assert _run(ws, "roadmap", "set", "A.3", "--status", "done") == 0
    assert yaml.safe_load(roadmap_file.read_text("utf-8"))["status"] == "done"


def test_task_command_writes_safe_yaml(tmp_path: Path) -> None:
    import yaml

    ws = tmp_path / "ws"
    assert _run(ws, "init", "--no-interactive") == 0
    _use_null_engines(ws)
    assert _run(ws, "project", "add", "ag-main", "projects/ag-main", "--build", "lake build") == 0

    objective = "Prove Foo.bar: it's the crux"  # apostrophe + colon
    assert _run(
        ws, "task", "add", "--id", "T-1", "--project", "ag-main",
        "--objective", objective, "--file", "Foo.lean", "--status", "queued",
    ) == 0
    task_file = ws / ".archon-horizon" / "tasks" / "items" / "T-1.yaml"
    data = yaml.safe_load(task_file.read_text("utf-8"))  # valid YAML
    assert data["id"] == "T-1" and data["objective"] == objective and data["status"] == "queued"

    assert _run(ws, "roadmap", "add", "--id", "A.3", "--title", "linked", "--project", "ag-main") == 0
    data["roadmap_refs"] = ["A.3"]
    task_file.write_text(yaml.safe_dump(data, sort_keys=False), "utf-8")

    assert _run(ws, "task", "set", "T-1", "--status", "done") == 0
    assert yaml.safe_load(task_file.read_text("utf-8"))["status"] == "done"
    roadmap_file = ws / ".archon-horizon" / "roadmap" / "items" / "A.3.yaml"
    assert yaml.safe_load(roadmap_file.read_text("utf-8"))["status"] == "done"
    comments_dir = ws / ".archon-horizon" / "roadmap" / "comments" / "A.3"
    assert any("T-1" in p.read_text("utf-8") for p in comments_dir.glob("*.md"))
    assert _run(ws, "task", "list") == 0
    assert _run(ws, "task", "remove", "T-1") == 0
    assert not task_file.exists()


def test_agents_cannot_author_tasks(tmp_path: Path, monkeypatch: Any) -> None:
    """Tasks are human-only: an agent run (ARCHON_HORIZON_AGENT_ROLE set) may read
    and comment, but add/set/remove are refused. Work is organized via the roadmap."""
    ws = tmp_path / "ws"
    assert _run(ws, "init", "--no-interactive") == 0
    _use_null_engines(ws)
    assert _run(ws, "project", "add", "ag-main", "projects/ag-main", "--build", "lake build") == 0
    # A human seeds the task.
    assert _run(ws, "task", "add", "--id", "T-1", "--project", "ag-main", "--objective", "x") == 0
    task_file = ws / ".archon-horizon" / "tasks" / "items" / "T-1.yaml"
    assert task_file.exists()

    # Now act as the horizon agent: authoring is refused, reading/commenting is not.
    monkeypatch.setenv("ARCHON_HORIZON_AGENT_ROLE", "horizon")
    assert _run(ws, "task", "add", "--id", "T-2", "--project", "ag-main", "--objective", "y") != 0
    assert not (ws / ".archon-horizon" / "tasks" / "items" / "T-2.yaml").exists()
    assert _run(ws, "task", "set", "T-1", "--status", "done") != 0
    assert _run(ws, "task", "remove", "T-1") != 0
    assert task_file.exists()  # untouched
    assert _run(ws, "task", "list") == 0
    assert _run(ws, "task", "comment", "T-1", "--body", "noted") == 0


def test_init_refuses_to_clobber(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    assert _run(ws, "init", "--no-interactive") == 0
    assert _run(ws, "init", "--no-interactive") == 1


def test_init_scaffolds_minimal_state_dirs(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    assert _run(ws, "init", "--no-interactive") == 0
    state_dirs = sorted(p.name for p in (ws / ".archon-horizon").iterdir() if p.is_dir())
    assert state_dirs == ["blueprints", "inbox", "roadmap", "runs", "subagents", "tasks", "tools", "vcs"]


def test_init_does_not_seed_starter_subagents(tmp_path: Path) -> None:
    # The bundled descriptors are the roster (merged at compile time); the
    # workspace subagents dir must start empty, not be polluted with a seeded set.
    ws = tmp_path / "ws"
    assert _run(ws, "init", "--no-interactive") == 0
    sub_dir = ws / ".archon-horizon" / "subagents"
    assert list(sub_dir.glob("*.md")) == []


def test_update_removes_legacy_seeded_subagents(tmp_path: Path) -> None:
    # A workspace an older Horizon seeded with legacy starter descriptors: --update
    # removes them (they are stale duplicates of the bundled roster).
    ws = tmp_path / "ws"
    assert _run(ws, "init", "--no-interactive") == 0
    sub_dir = ws / ".archon-horizon" / "subagents"
    (sub_dir / "blueprint-reviewer.md").write_text("---\nname: blueprint-reviewer\n---\nold\n", "utf-8")
    (sub_dir / "diff-auditor.md").write_text("---\nname: diff-auditor\n---\nold\n", "utf-8")
    (sub_dir / "my-custom.md").write_text("---\nname: my-custom\n---\nmine\n", "utf-8")

    assert _run(ws, "init", "--update", "--no-interactive") == 0

    assert not (sub_dir / "blueprint-reviewer.md").exists()
    assert not (sub_dir / "diff-auditor.md").exists()
    assert (sub_dir / "my-custom.md").exists()  # a user's own descriptor is untouched


def test_init_advisor_with_null_harness_writes_prompt_only(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    config = json.dumps({"ground_kind": "null", "horizon_kind": "null"})
    assert _run(ws, "init", "--config-json", config, "--advisor") == 0

    # The fallback prompt lives under runs, not in a global reports store.
    prompt = ws / ".archon-horizon" / "runs" / "post-init-advisor" / "prompt.md"
    assert prompt.exists()
    assert not (ws / ".archon-horizon" / "reports" / "post-init-advisor-prompt.md").exists()
    assert "interactive workspace advisor" in prompt.read_text("utf-8")


def test_init_advisor_launches_interactive_claude_backend(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    class Completed:
        returncode = 0

    def fake_run(argv: list[str], **kwargs: Any) -> Completed:
        calls.append((argv, kwargs))
        return Completed()

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("subprocess.run", fake_run)
    config = json.dumps(
        {
            "ground_kind": "claude-code",
            "ground_model": "sonnet",
            "horizon_kind": "null",
        }
    )

    assert _run(ws, "init", "--config-json", config, "--advisor") == 0

    argv, kwargs = next(call for call in calls if call[0][0] == "claude")
    assert argv[:3] == ["claude", "--model", "sonnet"]
    assert "-p" not in argv
    assert "interactive workspace advisor" in argv[-1]
    assert kwargs["cwd"] == ws


_MIN_CONFIG = """
workspace:
  name: w
  rounds: 1
  ground_agent: {harness: inf}
  horizon_agent: {harness: hor}
harnesses:
  inf: {kind: "null"}
  hor: {kind: "null"}
projects:
  ag-main: {path: projects/ag-main}
"""


def test_inbox_comment_and_hint_tag_verbs(tmp_path: Path) -> None:
    from archon_horizon.inboxes.filesystem import FilesystemInboxProvider

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(_MIN_CONFIG, "utf-8")

    # --persistent prepends the tag agents read; --project scopes the item.
    assert _run(ws, "inbox", "add", "--body", "use omega\n\nclose arithmetic goals with omega", "--persistent", "--project", "ag-main") == 0
    assert _run(ws, "inbox", "comment", "I-0001", "--body", "applied in lemma 3") == 0

    provider = FilesystemInboxProvider(ws / ".archon-horizon" / "inbox" / "local")
    item = provider.get_item("I-0001")
    assert item.body == "[persistent] use omega\n\nclose arithmetic goals with omega"
    assert item.scope.targets("projects") == ("ag-main",)
    assert item.metadata["comments"][0]["body"] == "applied in lemma 3"


def test_inbox_add_rejects_conflicting_tags(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(_MIN_CONFIG, "utf-8")
    assert _run(ws, "inbox", "add", "--body", "x", "--persistent", "--temporary") != 0
