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

from .schema import HarnessConfig

HarnessBuilder = Callable[[HarnessConfig], Harness]


class UnknownHarnessKind(ValueError):
    """Raised when a harness config names a kind with no registered builder."""


def _build_command(cfg: HarnessConfig) -> Harness:
    if not cfg.command:
        raise ValueError(f"harness {cfg.name!r} (kind={cfg.kind}) needs a 'command'")
    return CommandHarness(cfg.name, [cfg.command, *cfg.args])


def _build_claude_code(cfg: HarnessConfig) -> Harness:
    argv = ["claude", "-p"]
    if cfg.model:
        argv += ["--model", cfg.model]
    argv += [*cfg.args, PROMPT_TOKEN]
    return CommandHarness(cfg.name, argv)


def _build_codex(cfg: HarnessConfig) -> Harness:
    argv = ["codex", "exec", "--json", "--skip-git-repo-check"]
    if cfg.model:
        argv += ["-m", cfg.model]
    effort = cfg.options.get("effort")
    if effort:
        argv += ["-c", f"model_reasoning_effort={effort}"]
    argv += [*cfg.args, PROMPT_TOKEN]
    return CommandHarness(cfg.name, argv)


def _build_null(cfg: HarnessConfig) -> Harness:
    return NullHarness()


_DEFAULT_BUILDERS: dict[str, HarnessBuilder] = {
    "command": _build_command,
    "external-agent": _build_command,
    "claude-code": _build_claude_code,
    "codex": _build_codex,
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
