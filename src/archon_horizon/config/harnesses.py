"""Build :class:`Harness` instances from config — the config→engine bridge.

A registry maps a harness ``kind`` to a builder. Adding support for a new
engine is ``registry.register("my-engine", builder)`` plus the builder
function; no other code changes. The built-in builders cover a generic
``command`` engine and thin ``claude-code`` / ``codex`` presets that assemble
argv from ``model`` / ``options.effort``, plus ``null`` for tests.

The CLI flag details for claude/codex live here on purpose: this is the one
place that knows engine-specific invocation, so when a CLI changes its flags,
only this module changes.
"""

from __future__ import annotations

from collections.abc import Callable

from archon_horizon.harnesses.base import Harness
from archon_horizon.harnesses.command import PROMPT_TOKEN, CommandHarness
from archon_horizon.harnesses.null import NullHarness
from archon_horizon.transcript.parsers import parse_claude_line, parse_codex_line, parse_plain_line
from archon_horizon.transcript.pricing import pricing_from_mapping, with_usage_pricing

from .schema import HarnessConfig

HarnessBuilder = Callable[[HarnessConfig], Harness]


class UnknownHarnessKind(ValueError):
    """Raised when a harness config names a kind with no registered builder."""


def _build_command(cfg: HarnessConfig) -> Harness:
    if not cfg.command:
        raise ValueError(f"harness {cfg.name!r} (kind={cfg.kind}) needs a 'command'")
    parser = with_usage_pricing(parse_plain_line, pricing_from_mapping(cfg.options.get("pricing")))
    return CommandHarness(cfg.name, [cfg.command, *cfg.args], parser=parser)


def _build_claude_code(cfg: HarnessConfig) -> Harness:
    argv = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    if cfg.model:
        argv += ["--model", cfg.model]
    argv += [*cfg.args, PROMPT_TOKEN]
    
    env_overrides = {}
    backend = cfg.options.get("backend")
    if backend == "vscode":
        env_overrides["CLAUDE_CODE_ENTRYPOINT"] = "claude-vscode"
    elif backend == "desktop":
        env_overrides["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
        
    parser = with_usage_pricing(parse_claude_line, pricing_from_mapping(cfg.options.get("pricing")))
    return CommandHarness(cfg.name, argv, parser=parser, env_overrides=env_overrides)


def _build_codex(cfg: HarnessConfig) -> Harness:
    argv = ["codex", "exec", "--json", "--skip-git-repo-check"]
    if cfg.model:
        argv += ["-m", cfg.model]
    effort = cfg.options.get("effort")
    if effort:
        argv += ["-c", f"model_reasoning_effort={effort}"]
    argv += [*cfg.args, PROMPT_TOKEN]
    parser = with_usage_pricing(parse_codex_line, pricing_from_mapping(cfg.options.get("pricing")))
    return CommandHarness(cfg.name, argv, parser=parser)


def _build_antigravity(cfg: HarnessConfig) -> Harness:
    argv = ["agy", "headless"]
    if cfg.model:
        argv += ["--model", cfg.model]
    argv += [*cfg.args, PROMPT_TOKEN]
    parser = with_usage_pricing(parse_plain_line, pricing_from_mapping(cfg.options.get("pricing")))
    return CommandHarness(cfg.name, argv, parser=parser)


def _build_null(cfg: HarnessConfig) -> Harness:
    return NullHarness()


_DEFAULT_BUILDERS: dict[str, HarnessBuilder] = {
    "command": _build_command,
    "external-agent": _build_command,
    "claude-code": _build_claude_code,
    "codex": _build_codex,
    "antigravity": _build_antigravity,
    "null": _build_null,
}


class HarnessRegistry:
    def __init__(self, builders: dict[str, HarnessBuilder] | None = None) -> None:
        self._builders = dict(_DEFAULT_BUILDERS if builders is None else builders)

    def register(self, kind: str, builder: HarnessBuilder) -> None:
        self._builders[kind] = builder

    def build(self, cfg: HarnessConfig) -> Harness:
        try:
            builder = self._builders[cfg.kind]
        except KeyError as exc:
            raise UnknownHarnessKind(
                f"no builder for harness kind {cfg.kind!r} "
                f"(registered: {sorted(self._builders)})"
            ) from exc
        return builder(cfg)

    def build_all(self, configs: dict[str, HarnessConfig]) -> dict[str, Harness]:
        return {name: self.build(cfg) for name, cfg in configs.items()}
