"""Project-local environment loading and Claude-compatible provider routing."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path


PROVIDERS: dict[str, tuple[str, ...]] = {
    "moonshot": ("MOONSHOT_API_KEY", "KIMI_API_KEY", "MOONSHOT_BASE_URL", "MOONSHOT_MODEL"),
    "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"),
    "openrouter": ("OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "OPENROUTER_MODEL"),
}

TEMPLATE_DEFAULTS: dict[str, str] = {
    "MOONSHOT_BASE_URL": "https://api.kimi.com/coding/",
    "MOONSHOT_MODEL": "kimi-k2.6",
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com/anthropic",
    "DEEPSEEK_MODEL": "deepseek-coder",
    "OPENROUTER_BASE_URL": "https://openrouter.ai/api",
}

PROVIDER_ALIASES: dict[str, str] = {
    "kimi": "moonshot",
    "moonshot": "moonshot",
    "deepseek": "deepseek",
    "openrouter": "openrouter",
}

OPENROUTER_FALLBACK_MODELS: dict[str, str] = {
    "moonshot": "moonshotai/kimi-k2",
    "deepseek": "deepseek/deepseek-r1",
}


def env_path(root: Path) -> Path:
    return root / ".env"


def load_env_file(root: Path) -> dict[str, str]:
    """Load ``<root>/.env`` as fallback environment variables.

    Existing shell variables win. Empty values are ignored, so scaffolded
    placeholders cannot clobber real credentials.
    """
    path = env_path(root)
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    for raw in path.read_text("utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or not value:
            continue
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


def render_env_template(*, shell_env: Mapping[str, str] | None = None) -> str:
    src = shell_env if shell_env is not None else os.environ
    keys = (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "MOONSHOT_API_KEY",
        "KIMI_API_KEY",
        "MOONSHOT_BASE_URL",
        "MOONSHOT_MODEL",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL",
        "OPENROUTER_API_KEY",
        "OPENROUTER_BASE_URL",
        "OPENROUTER_MODEL",
    )

    def line(key: str) -> str:
        value = src.get(key, "")
        if value:
            return f"{key}={value}"
        default = TEMPLATE_DEFAULTS.get(key, "")
        if default:
            return f"# {key}={default}"
        return f"# {key}="

    return (
        "# Archon Horizon environment.\n"
        "#\n"
        "# This file is loaded as a fallback before harnesses are built.\n"
        "# Existing shell variables always win; never commit real API keys.\n"
        "#\n"
        "# Kimi/Moonshot and DeepSeek are routed through Claude Code's\n"
        "# Anthropic-compatible environment variables at runtime.\n\n"
        + "\n".join(line(key) for key in keys)
        + "\n"
    )


def write_env_template(root: Path, *, force: bool = False) -> bool:
    path = env_path(root)
    if path.exists() and not force:
        return False
    path.write_text(render_env_template(), "utf-8")
    return True


def _first_env(keys: Iterable[str]) -> tuple[str | None, str | None]:
    for key in keys:
        value = os.environ.get(key)
        if value:
            return key, value
    return None, None


def provider_env(provider: str, *, model: str | None = None) -> dict[str, str] | None:
    provider = PROVIDER_ALIASES.get(provider, provider)
    keys = PROVIDERS.get(provider)
    if not keys:
        return None
    api_keys = keys[:-2]
    base_key = keys[-2]
    model_key = keys[-1]
    _, api_key = _first_env(api_keys)
    if not api_key:
        return None
    resolved_model = model or os.environ.get(model_key) or TEMPLATE_DEFAULTS.get(model_key, "")
    env: dict[str, str] = {
        "ANTHROPIC_BASE_URL": os.environ.get(base_key) or TEMPLATE_DEFAULTS.get(base_key, ""),
        "ANTHROPIC_AUTH_TOKEN": api_key,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    if provider == "openrouter":
        env["ANTHROPIC_API_KEY"] = ""
    if resolved_model:
        env.update(
            {
                "ANTHROPIC_MODEL": resolved_model,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": resolved_model,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": resolved_model,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": resolved_model,
            }
        )
    return env


def openrouter_fallback_env(provider: str) -> dict[str, str] | None:
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    model = OPENROUTER_FALLBACK_MODELS.get(PROVIDER_ALIASES.get(provider, provider))
    if not openrouter_key or not model:
        return None
    base_url = os.environ.get("OPENROUTER_BASE_URL") or TEMPLATE_DEFAULTS["OPENROUTER_BASE_URL"]
    return {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": openrouter_key,
        "ANTHROPIC_API_KEY": "",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
    }


def missing_key_message(provider: str, model: str | None) -> str:
    provider = PROVIDER_ALIASES.get(provider, provider)
    keys = PROVIDERS.get(provider, (f"{provider.upper()}_API_KEY",))
    names = ", ".join(keys[:-2] or keys[:1])
    label = model or provider
    return (
        f"model/provider {label!r} needs {names}; add it to .env or export it "
        "in the shell before running the harness"
    )
