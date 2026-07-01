"""Compiling one descriptor into engine-native subagents (Claude .md / Codex .toml)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

from archon_horizon.config.schema import HarnessConfig
from archon_horizon.subagents.base import SubagentDescriptor
from archon_horizon.subagents.compile import (
    compile_for_claude,
    install_subagents,
    render_claude_agent,
    render_codex_agent,
    resolve_model,
)

_FM = re.compile(r"^---\s*\n(.*?\n)---\s*\n", re.DOTALL)

CLAUDE = HarnessConfig.from_raw("g", {"kind": "claude-code", "model": "opus", "models": {"small": "haiku"}})
CODEX = HarnessConfig.from_raw("c", {"kind": "codex", "model": "gpt-5", "models": {"small": "gpt-5-mini"}})


def _desc(**kw) -> SubagentDescriptor:
    base = dict(name="rev", description="Review the diff for \\uses errors.", prompt_body="Body with \\lean{Foo}.")
    base.update(kw)
    return SubagentDescriptor(**base)


def test_model_precedence_explicit_then_tier_then_inherit() -> None:
    assert resolve_model(_desc(model="sonnet", tier="small"), CLAUDE) == "sonnet"  # explicit wins
    assert resolve_model(_desc(tier="small"), CLAUDE) == "haiku"                   # tier resolved
    assert resolve_model(_desc(), CLAUDE) is None                                  # neither → inherit


def test_claude_agent_is_valid_frontmatter_and_omits_model_to_inherit() -> None:
    md = render_claude_agent(_desc(), CLAUDE)
    fm = yaml.safe_load(_FM.match(md).group(1))
    assert fm["name"] == "rev"
    assert "model" not in fm                       # no tier/model → inherit parent
    assert md.rstrip().endswith("Body with \\lean{Foo}.")  # backslashes preserved


def test_codex_agent_is_valid_toml_with_latex_backslashes() -> None:
    toml = render_codex_agent(_desc(tier="small"), CODEX)
    data = tomllib.loads(toml)
    assert data["name"] == "rev"
    assert data["model"] == "gpt-5-mini"                      # tier resolved for codex
    assert "\\lean{Foo}" in data["developer_instructions"]    # not mangled by TOML escaping
    assert "\\uses" in data["description"]


def test_read_only_maps_to_native_enforcement() -> None:
    assert "disallowedTools: [Edit, Write, NotebookEdit]" in render_claude_agent(_desc(read_only=True), CLAUDE)
    assert 'sandbox_mode = "read-only"' in render_codex_agent(_desc(read_only=True), CODEX)


def test_writes_are_non_destructive_to_hand_authored_files(tmp_path: Path) -> None:
    dest = tmp_path / ".claude" / "agents"
    dest.mkdir(parents=True)
    # A human-authored agent with the same name (no generated-marker) must survive.
    hand = dest / "rev.md"
    hand.write_text("---\nname: rev\n---\nhuman version\n", "utf-8")
    written = compile_for_claude([_desc()], CLAUDE, dest)
    assert written == []                       # skipped, not clobbered
    assert hand.read_text("utf-8") == "---\nname: rev\n---\nhuman version\n"
    # Our own generated file is refreshed in place.
    dest2 = tmp_path / "fresh"
    assert compile_for_claude([_desc()], CLAUDE, dest2) == ["rev"]
    assert compile_for_claude([_desc(description="changed")], CLAUDE, dest2) == ["rev"]
    assert "changed" in (dest2 / "rev.md").read_text("utf-8")


def test_install_targets_workspace_local_dirs_per_engine(tmp_path: Path) -> None:
    harnesses = {
        "g": HarnessConfig.from_raw("g", {"kind": "claude-code", "model": "opus"}),
        "h": HarnessConfig.from_raw("h", {"kind": "codex", "model": "gpt-5"}),
    }
    out = install_subagents(tmp_path, tmp_path / "no-workspace-descriptors", harnesses)
    # Built-in roster compiled into BOTH engines' workspace-local agent dirs.
    assert out["claude"] and out["codex"]
    assert (tmp_path / ".claude" / "agents" / "work-reviewer.md").exists()
    assert (tmp_path / ".codex" / "agents" / "work-reviewer.toml").exists()
