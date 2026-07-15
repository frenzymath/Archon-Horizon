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
import json
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


# claude -p exposes a native ``--effort`` flag (low|medium|high|xhigh|max) — the
# proper reasoning-effort control, mirroring Codex's ``model_reasoning_effort``.
# A raw-integer effort is instead read as an explicit extended-thinking token
# budget (``MAX_THINKING_TOKENS``), the fine-grained escape hatch. ``ultracode``
# is NOT a --effort value: it is a Claude Code *session setting* (xhigh +
# automatic dynamic-workflow orchestration). Per the Claude Code docs it is
# enabled — including headlessly under ``claude -p`` — by passing
# ``{"ultracode": true}`` through ``--settings`` (never via --effort /
# effortLevel / CLAUDE_CODE_EFFORT_LEVEL). So it drives no ``--effort`` flag and
# is wired in through :func:`_claude_ultracode` instead.
CLAUDE_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


# Effort values that mean "no override — let the engine pick its own default".
# Scaffolding ``effort: default`` keeps the knob discoverable in config.yaml
# without forcing a reasoning tier onto the run.
_EFFORT_DEFAULT_SENTINELS = frozenset({"", "default", "none", "auto"})


def _effort_label(cfg: HarnessConfig) -> str | None:
    """The configured effort as a display string, or ``None`` when unset or set
    to a 'use the engine default' sentinel (``default``/``none``/``auto``).

    Engine-agnostic: both Codex (``model_reasoning_effort``) and Claude Code
    (thinking budget) read the same ``options.effort`` key, so the Logs/run view
    can show it uniformly. Normalised to lower-case; a raw integer budget is kept
    verbatim (e.g. ``"9000"``)."""
    raw = cfg.options.get("effort")
    if raw is None:
        return None
    value = str(raw).strip().lower()
    return None if value in _EFFORT_DEFAULT_SENTINELS else value


def _claude_effort_flag(cfg: HarnessConfig) -> str | None:
    """The native ``--effort`` level for claude -p, or ``None`` to leave claude's
    own default. Named tiers pass through; ``ultracode`` is a session setting, not
    a flag value, so it returns ``None`` here and is applied via ``--settings``
    instead (see :func:`_claude_ultracode`); a raw-integer effort is an
    extended-thinking budget rather than an effort level, so it also returns
    ``None`` here (see :func:`_claude_thinking_budget`). Raises on any other value
    so a typo fails loudly instead of being ignored.
    """
    effort = _effort_label(cfg)
    if effort is None or effort.isdigit():
        return None
    if effort == "ultracode":
        # Not a --effort value: applied via --settings '{"ultracode": true}' (see
        # _claude_ultracode). The setting already sends xhigh, so no --effort flag.
        return None
    if effort in CLAUDE_EFFORT_LEVELS:
        return effort
    raise ValueError(
        f"harness {cfg.name!r}: unknown effort {effort!r}; expected default or one of "
        f"{', '.join(CLAUDE_EFFORT_LEVELS)}, or an integer thinking-token budget"
    )


def _claude_thinking_budget(cfg: HarnessConfig) -> int | None:
    """A raw-integer ``options.effort`` as an explicit ``MAX_THINKING_TOKENS``
    budget — the fine-grained escape hatch that complements ``--effort``. Named
    tiers drive the flag instead, so they yield ``None`` here. Returns ``None``
    when effort is unset or non-numeric."""
    effort = _effort_label(cfg)
    if effort is None or not effort.isdigit():
        return None
    return int(effort)


def _claude_ultracode(cfg: HarnessConfig) -> bool:
    """True when ``options.effort`` is ``ultracode`` — Claude Code's session mode
    that pairs ``xhigh`` reasoning with automatic dynamic-workflow orchestration.

    It is not a ``--effort`` level; per the Claude Code docs it is enabled by
    passing ``{"ultracode": true}`` through ``--settings``, which works in
    headless ``claude -p``. :func:`_build_claude_code` injects that setting rather
    than an ``--effort`` flag. Requires workflows enabled and claude >= 2.1.154."""
    return _effort_label(cfg) == "ultracode"


# Provider API-key env vars per engine kind: if one is set (in the harness's own
# ``env`` or the ambient environment) the CLI authenticates by API key (metered
# billing); otherwise it uses the stored subscription/OAuth credentials in its
# config dir. Best-effort — enough to show "api-key" vs "subscription" in the UI.
_AUTH_KEY_VARS: dict[str, tuple[str, ...]] = {
    "claude-code": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "codex": ("OPENAI_API_KEY",),
}


def _auth_mode(cfg: HarnessConfig) -> str | None:
    """Best-effort auth mode for a harness: ``api-key`` when a provider key is in
    scope, else ``subscription``; ``None`` for kinds we don't recognise."""
    import os

    key_vars = _AUTH_KEY_VARS.get(cfg.kind)
    if not key_vars:
        return None
    env = _env_overrides(cfg)
    present = any(env.get(v) or os.environ.get(v) for v in key_vars)
    return "api-key" if present else "subscription"


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

    # Reasoning effort → native `--effort` flag (appended to argv below). A
    # raw-integer effort is instead an explicit extended-thinking budget; an
    # explicit MAX_THINKING_TOKENS in the harness `env` always wins (setdefault).
    effort_flag = _claude_effort_flag(cfg)
    budget = _claude_thinking_budget(cfg)
    if budget is not None:
        env_overrides.setdefault("MAX_THINKING_TOKENS", str(budget))

    # `ultracode` is a session setting, not an --effort level: enable it the way
    # the Claude Code docs prescribe — `--settings '{"ultracode": true}'`, which
    # works headlessly under `claude -p`. It already sends xhigh, so effort_flag
    # is None for it (see _claude_effort_flag). Injected into argv below.
    ultracode_settings = json.dumps({"ultracode": True}) if _claude_ultracode(cfg) else None
    if ultracode_settings is not None:
        log.info(
            f"harness {cfg.name!r}: ultracode enabled — xhigh + automatic dynamic-workflow "
            "orchestration via --settings (needs workflows enabled and claude >= 2.1.154; "
            "each substantive task may fan out to many agents)."
        )

    # Horizon agents run fully headless — there is no TTY to answer a permission
    # prompt, so Claude must bypass them or every `cd`/redirect/multi-op command
    # and out-of-tree write is denied. Horizon's own write-domain/freeze checks
    # are the safety net. Set `options.skip_permissions: false` to opt out.
    skip_perms = bool(cfg.options.get("skip_permissions", True))

    backend = str(cfg.options.get("backend") or "default").strip().lower()
    if backend == "interactive":
        # Interactive is a run *mode*, not a headless transport. A single-role run
        # (`horizon run horizon`/`ground`, or `--backend interactive`) launches this
        # harness as an interactive TTY via commands/interactive.py; run.py routes
        # there when the role's harness declares `backend: interactive`. When the
        # SAME harness is built for the headless orchestrated alternation, fall back
        # to the default transport so the run still works.
        log.info(
            f"harness {cfg.name!r}: backend 'interactive' applies to single-role runs "
            "(launched as a TTY session); using the default transport for headless orchestration."
        )
        backend = "default"
    if backend == "claude-p":
        if shutil.which("claude-p") is None:
            log.warn(CLAUDE_P_INSTALL_HINT)
        argv = ["claude-p", PROMPT_TOKEN, "--output-format", "stream-json", "--verbose"]
        if model:
            argv += ["--model", model]
        if effort_flag and _claude_p_supports("--effort"):
            argv += ["--effort", effort_flag]
        if ultracode_settings and _claude_p_supports("--settings"):
            argv += ["--settings", ultracode_settings]
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
    if effort_flag:
        argv += ["--effort", effort_flag]
    if ultracode_settings:
        argv += ["--settings", ultracode_settings]
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
    # `effort: default` (and other sentinels) leave Codex's own default rather
    # than passing an override it would reject.
    effort = _effort_label(cfg)
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
        setattr(harness, "horizon_effort", _effort_label(cfg))
        # Provenance for the Logs view: which engine config-home the session used
        # and how it authenticated (api-key vs subscription).
        setattr(harness, "horizon_config_dir", _config_dir(cfg))
        setattr(harness, "horizon_auth", _auth_mode(cfg))
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
