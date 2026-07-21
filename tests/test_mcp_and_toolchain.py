"""Phase 8: MCP config generation and Lean toolchain detection."""

from __future__ import annotations

import json
from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.config.loader import build_orchestrator
from archon_horizon.config.mcp import (
    codex_mcp_toml_block,
    default_mcp_servers,
    install_mcp_for_harnesses,
    merge_mcp_config,
    write_codex_mcp_config,
    write_mcp_config,
)
from archon_horizon.config.schema import HarnessConfig
from archon_horizon.config.toolchain import lean_toolchain_report, missing_lean_tools
from archon_horizon.core.roadmap import Roadmap, RoadmapItem, RoadmapStatus
from archon_horizon.core.sessions import RunRecord
from archon_horizon.harnesses.null import NullHarness


def test_default_servers_include_lean_lsp() -> None:
    assert "lean-lsp" in default_mcp_servers()


def test_merge_preserves_existing_servers() -> None:
    existing = {"mcpServers": {"custom": {"command": "x"}}}
    merged = merge_mcp_config(existing, default_mcp_servers())
    assert merged["mcpServers"]["custom"] == {"command": "x"}  # not clobbered
    assert "lean-lsp" in merged["mcpServers"]


def test_merge_refreshes_managed_leansearch_after_interpreter_move() -> None:
    old = {
        "mcpServers": {
            "leansearch": {
                "command": "/old/venv/bin/python",
                "args": ["-m", "archon_horizon.search.mcp_server"],
            }
        }
    }
    new = {
        "leansearch": {
            "command": "/new/venv/bin/python",
            "args": ["-m", "archon_horizon.search.mcp_server"],
        }
    }
    assert merge_mcp_config(old, new)["mcpServers"]["leansearch"] == new["leansearch"]


def test_merge_preserves_custom_server_named_leansearch() -> None:
    custom = {"command": "custom-search", "args": ["--stdio"]}
    existing = {"mcpServers": {"leansearch": custom}}
    merged = merge_mcp_config(existing, default_mcp_servers())
    assert merged["mcpServers"]["leansearch"] == custom


def test_codex_mcp_toml_block_shape() -> None:
    block = codex_mcp_toml_block("leansearch", {"command": "python", "args": ["-m", "x.y"]})
    assert block == '[mcp_servers.leansearch]\ncommand = "python"\nargs = ["-m", "x.y"]\n'


def test_write_codex_mcp_config_is_project_local_and_non_destructive(tmp_path: Path) -> None:
    codex_dir = tmp_path / ".codex"
    # Pre-existing config with a hand-added server and an unrelated table.
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(
        'model = "gpt-5"\n\n[mcp_servers.custom]\ncommand = "x"\nargs = []\n', "utf-8"
    )
    added = write_codex_mcp_config(codex_dir)
    assert set(added) == set(default_mcp_servers())  # ours appended
    import tomllib

    data = tomllib.loads((codex_dir / "config.toml").read_text("utf-8"))
    assert data["model"] == "gpt-5"                       # unrelated config preserved
    assert data["mcp_servers"]["custom"] == {"command": "x", "args": []}  # hand-added kept
    assert "lean-lsp" in data["mcp_servers"]              # ours present
    # Idempotent: a second pass adds nothing.
    assert write_codex_mcp_config(codex_dir) == []


def test_write_codex_mcp_config_refreshes_managed_leansearch(tmp_path: Path) -> None:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    path = codex_dir / "config.toml"
    path.write_text(
        '# keep this comment\n\n[mcp_servers.leansearch]\n'
        'command = "/old/venv/bin/python"\n'
        'args = ["-m", "archon_horizon.search.mcp_server"]\n\n'
        '[unrelated]\nvalue = 1\n',
        "utf-8",
    )
    servers = {
        "leansearch": {
            "command": "/new/venv/bin/python",
            "args": ["-m", "archon_horizon.search.mcp_server"],
        }
    }

    assert write_codex_mcp_config(codex_dir, servers) == ["leansearch"]
    text = path.read_text("utf-8")
    assert 'command = "/new/venv/bin/python"' in text
    assert "# keep this comment" in text
    assert "[unrelated]\nvalue = 1" in text
    assert write_codex_mcp_config(codex_dir, servers) == []


def test_write_codex_mcp_config_preserves_custom_leansearch(tmp_path: Path) -> None:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    path = codex_dir / "config.toml"
    original = (
        "[mcp_servers.leansearch]\n"
        'command = "custom-search"\n'
        'args = ["--stdio"]\n'
    )
    path.write_text(original, "utf-8")

    servers = {
        "leansearch": {
            "command": "/new/venv/bin/python",
            "args": ["-m", "archon_horizon.search.mcp_server"],
        }
    }
    assert write_codex_mcp_config(codex_dir, servers) == []
    assert path.read_text("utf-8") == original


def test_install_mcp_for_harnesses_writes_codex_project_config(tmp_path: Path) -> None:
    configs = {
        "g": HarnessConfig(name="g", kind="claude-code"),  # handled by project .mcp.json, skipped here
        "h": HarnessConfig(name="h", kind="codex"),
    }
    out = install_mcp_for_harnesses(configs, tmp_path)
    assert "claude-code" not in " ".join(out)  # claude not registered here
    assert set(out["codex"]) == set(default_mcp_servers())
    assert (tmp_path / ".codex" / "config.toml").exists()


def test_write_mcp_config_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    names = write_mcp_config(path)
    assert "lean-lsp" in names
    data = json.loads(path.read_text("utf-8"))
    assert data["mcpServers"]["lean-lsp"]["command"] == "uvx"
    # Re-running is idempotent and preserves a hand-added server.
    data["mcpServers"]["mine"] = {"command": "y"}
    path.write_text(json.dumps(data), "utf-8")
    write_mcp_config(path)
    after = json.loads(path.read_text("utf-8"))
    assert "mine" in after["mcpServers"] and "lean-lsp" in after["mcpServers"]


def test_write_mcp_config_refreshes_managed_leansearch(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps({
            "mcpServers": {
                "leansearch": {
                    "command": "/old/venv/bin/python",
                    "args": ["-m", "archon_horizon.search.mcp_server"],
                },
                "mine": {"command": "custom-search"},
            }
        }),
        "utf-8",
    )
    servers = {
        "leansearch": {
            "command": "/new/venv/bin/python",
            "args": ["-m", "archon_horizon.search.mcp_server"],
        }
    }

    write_mcp_config(path, servers)
    refreshed = json.loads(path.read_text("utf-8"))["mcpServers"]
    assert refreshed["leansearch"]["command"] == "/new/venv/bin/python"
    assert refreshed["mine"] == {"command": "custom-search"}


def test_lean_toolchain_report_and_missing() -> None:
    present = {"elan": "/u/elan", "lean": "/u/lean", "lake": "/u/lake"}
    report = lean_toolchain_report(lambda t: present.get(t))
    assert report == {"elan": True, "lean": True, "lake": True}
    assert missing_lean_tools(report) == ()

    partial = lean_toolchain_report(lambda t: "/u/lean" if t == "lean" else None)
    assert missing_lean_tools(partial) == ("elan", "lake")


def test_init_writes_mcp_and_skills(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    assert main(["--root", str(ws), "init", "--no-interactive"]) == 0
    assert (ws / ".mcp.json").exists()
    assert json.loads((ws / ".mcp.json").read_text("utf-8"))["mcpServers"]["lean-lsp"]
    assert (ws / ".claude" / "skills" / "hgraph" / "SKILL.md").exists()


_BP_CONFIG = """
workspace:
  name: w
  rounds: 1
  ground_agent: {harness: inf, subagents: []}
  horizon_agent: {harness: hor}
harnesses:
  inf: {kind: "null"}
  hor: {kind: "null"}
projects:
  ag-main:
    path: projects/ag-main
    blueprint: {path: projects/ag-main/blueprint}
"""


def test_run_refreshes_blueprint_dag(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    bp = root / "projects" / "ag-main" / "blueprint"
    bp.mkdir(parents=True)
    (bp / "ch1.tex").write_text(r"\begin{lemma}\label{x}\lean{X}\leanok X.\end{lemma}", "utf-8")
    (root / "config.yaml").write_text(_BP_CONFIG, "utf-8")

    orch = build_orchestrator(root, harnesses={"inf": NullHarness(""), "hor": NullHarness("")})
    orch.roadmap_store.save(
        Roadmap(items=(RoadmapItem(id="R-1", title="x", projects=("ag-main",), status=RoadmapStatus.ACTIVE),))
    )
    orch.run(RunRecord(id="", rounds_requested=1))

    dag = root / ".archon-horizon" / "blueprints" / "ag-main.json"
    assert dag.exists()
    assert json.loads(dag.read_text("utf-8"))["nodes"][0]["id"] == "x"
