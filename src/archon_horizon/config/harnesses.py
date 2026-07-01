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
import functools
import shutil
import subprocess

from archon_horizon.harnesses.base import Harness
from archon_horizon.harnesses.codex import CodexHarness
from archon_horizon.harnesses.command import PROMPT_TOKEN, CommandHarness
from archon_horizon.harnesses.null import NullHarness
from archon_horizon.log import log
from archon_horizon.transcript.parsers import (
    claude_session_id,
    codex_session_id,
    parse_claude_line,
    parse_codex_line,
    parse_plain_line,
)
from archon_horizon.transcript.pricing import pricing_from_mapping, with_usage_pricing

from .env import (
    PROVIDER_ALIASES,
    missing_key_message,
    openrouter_fallback_env,
    provider_env,
)
from .schema import HarnessConfig

HarnessBuilder = Callable[[HarnessConfig], Harness]
CLAUDE_P_INSTALL_HINT = (
    "claude-p is not installed. Install the maintained fork for the optional "
    "Claude Code TUI backend:\n"
    "  uv tool install --force git+https://github.com/AxelDlv00/claude-p\n"
    "  (or: pip install git+https://github.com/AxelDlv00/claude-p)"
)


class UnknownHarnessKind(ValueError):
    """Raised when a harness config names a kind with no registered builder."""


def _build_command(cfg: HarnessConfig) -> Harness:
    if not cfg.command:
        raise ValueError(f"harness {cfg.name!r} (kind={cfg.kind}) needs a 'command'")
    parser = with_usage_pricing(parse_plain_line, pricing_from_mapping(cfg.options.get("pricing")))
    return CommandHarness(cfg.name, [cfg.command, *cfg.args], parser=parser, env_overrides=_env_overrides(cfg))


def _env_overrides(cfg: HarnessConfig) -> dict[str, str]:
    raw = cfg.options.get("env", {})
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _config_dir(cfg: HarnessConfig) -> str | None:
    """The per-harness config/home dir override (see ``HarnessConfig.config_dir``)."""
    return cfg.config_dir


@functools.lru_cache(maxsize=1)
def _claude_p_help() -> str:
    try:
        out = subprocess.run(["claude-p", "--help"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out.stdout or "") + (out.stderr or "")


def _claude_p_supports(flag: str) -> bool:
    return flag in _claude_p_help()


def _resolve_claude_provider(cfg: HarnessConfig) -> tuple[str | None, dict[str, str]]:
    model = cfg.model.strip() if isinstance(cfg.model, str) and cfg.model.strip() else None
    explicit_provider = cfg.options.get("provider")
    provider = str(explicit_provider).strip().lower() if explicit_provider else None
    alias_provider = PROVIDER_ALIASES.get((model or "").lower())
    provider = PROVIDER_ALIASES.get(provider or "", provider) or alias_provider
    if provider is None:
        return model, {}

    # With an explicit provider, a non-alias model means "use this concrete
    # provider model"; with an alias like `model: kimi`, keep provider defaults.
    provider_model = str(cfg.options.get("provider_model") or "").strip() or None
    if provider_model is None and explicit_provider and model and model.lower() not in PROVIDER_ALIASES:
        provider_model = model

    env = provider_env(provider, model=provider_model)
    if not env and provider != "openrouter":
        env = openrouter_fallback_env(provider)
    if not env:
        raise ValueError(missing_key_message(provider, model))

    real_model = provider_model or env.get("ANTHROPIC_MODEL") or model
    return real_model, env


def _build_claude_code(cfg: HarnessConfig) -> Harness:
    model, provider_env_vars = _resolve_claude_provider(cfg)
    env_overrides = {**provider_env_vars, **_env_overrides(cfg)}
    parser = with_usage_pricing(parse_claude_line, pricing_from_mapping(cfg.options.get("pricing")))

    # CLAUDE_CONFIG_DIR governs config, auth, skills (user-level), and sessions —
    # set it from the unified config_dir so multi-account setups stay isolated.
    config_dir = _config_dir(cfg)
    if config_dir:
        env_overrides.setdefault("CLAUDE_CONFIG_DIR", config_dir)

    # Horizon agents run fully headless — there is no TTY to answer a permission
    # prompt, so Claude must bypass them or every `cd`/redirect/multi-op command
    # and out-of-tree write is denied. Horizon's own write-domain/freeze checks
    # are the safety net. Set `options.skip_permissions: false` to opt out.
    skip_perms = bool(cfg.options.get("skip_permissions", True))

    backend = str(cfg.options.get("backend") or "default").strip().lower()
    if backend == "interactive":
        raise ValueError("claude-code backend 'interactive' is not supported by Horizon's transcript harness")
    if backend == "claude-p":
        if shutil.which("claude-p") is None:
            log.warn(CLAUDE_P_INSTALL_HINT)
        argv = ["claude-p", PROMPT_TOKEN, "--output-format", "stream-json", "--verbose"]
        if model:
            argv += ["--model", model]
        timeout_sec = int(cfg.options.get("timeout_sec") or 1800)
        quiet_after_sec = int(cfg.options.get("quiet_after_sec") or 15)
        argv += [*cfg.args, "--timeout-sec", str(timeout_sec), "--quiet-after-sec", str(quiet_after_sec)]
        if skip_perms and _claude_p_supports("--dangerously-skip-permissions"):
            argv.append("--dangerously-skip-permissions")
        if _claude_p_supports("--trust-workspace"):
            argv.append("--trust-workspace")
        if _claude_p_supports("--isolate-config-dir"):
            argv.append("--isolate-config-dir")
        if _claude_p_supports("--stream-session-events"):
            argv.append("--stream-session-events")
        elif _claude_p_supports("--live-tui-deltas"):
            argv.append("--live-tui-deltas")
        config_dir = _config_dir(cfg)
        if config_dir:
            env_overrides["CLAUDE_CONFIG_DIR"] = config_dir
        harness = CommandHarness(
            cfg.name, argv, parser=parser, env_overrides=env_overrides,
            session_id_of=claude_session_id, resume_args=_claude_resume_args,
        )
        harness.horizon_model = model
        return harness

    argv = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    if skip_perms:
        argv.append("--dangerously-skip-permissions")
    if model:
        argv += ["--model", model]
    argv += [*cfg.args, PROMPT_TOKEN]

    if backend == "vscode":
        env_overrides["CLAUDE_CODE_ENTRYPOINT"] = "claude-vscode"
    elif backend == "desktop":
        env_overrides["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
    elif backend != "default":
        raise ValueError("unknown claude-code backend {!r}; expected default, vscode, desktop, or claude-p".format(backend))

    harness = CommandHarness(
        cfg.name, argv, parser=parser, env_overrides=env_overrides,
        session_id_of=claude_session_id, resume_args=_claude_resume_args,
    )
    harness.horizon_model = model
    return harness


def _claude_resume_args(session_id: str) -> list[str]:
    """Claude Code continues a prior conversation with ``--resume <id>``."""
    return ["--resume", session_id]


def _build_codex(cfg: HarnessConfig) -> Harness:
    argv = ["codex", "exec", "--json", "--skip-git-repo-check"]
    if cfg.model:
        argv += ["-m", cfg.model]
    effort = cfg.options.get("effort")
    if effort:
        argv += ["-c", f"model_reasoning_effort={effort}"]
    # Headless: codex's default sandbox blocks the shell, so the agent can't run
    # `lake build`/git/etc and stalls. Honour an explicit `options.sandbox`
    # (read-only|workspace-write|danger-full-access); otherwise bypass entirely.
    sandbox = str(cfg.options.get("sandbox") or "").strip()
    if sandbox:
        argv += ["--sandbox", sandbox]
    elif bool(cfg.options.get("bypass_sandbox", True)):
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    argv += [*cfg.args, PROMPT_TOKEN]
    parser = with_usage_pricing(parse_codex_line, pricing_from_mapping(cfg.options.get("pricing")))
    env_overrides = _env_overrides(cfg)
    config_dir = _config_dir(cfg)
    if config_dir:
        # CODEX_HOME governs codex config, auth, and the sessions/ rollout logs.
        env_overrides.setdefault("CODEX_HOME", config_dir)
    return CodexHarness(
        cfg.name, argv, parser=parser, env_overrides=env_overrides,
        session_id_of=codex_session_id, codex_home=config_dir,
    )


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
        harness = builder(cfg)
        setattr(harness, "horizon_harness_name", cfg.name)
        setattr(harness, "horizon_harness_kind", cfg.kind)
        setattr(harness, "horizon_model", getattr(harness, "horizon_model", cfg.model))
        # Optional per-harness retry tuning for transient API errors.
        if "max_retries" in cfg.options:
            try:
                setattr(harness, "retry_max", max(0, int(cfg.options["max_retries"])))
            except (TypeError, ValueError):
                pass
        if "retry_base_seconds" in cfg.options:
            try:
                setattr(harness, "retry_base_seconds", max(0.0, float(cfg.options["retry_base_seconds"])))
            except (TypeError, ValueError):
                pass
        return harness

    def build_all(self, configs: dict[str, HarnessConfig]) -> dict[str, Harness]:
        return {name: self.build(cfg) for name, cfg in configs.items()}
