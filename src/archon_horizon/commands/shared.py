"""Shared command helpers."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from archon_horizon.config.loader import build_workspace, load_config
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider
from archon_horizon.inboxes.github import GithubInboxProvider

# The flag every command exposes for machine-readable output. The CLI callback
# also routes the banner and all human chrome to stderr when it is present, so
# stdout stays pure JSON.
JSON_OPTION_NAMES = ("--json",)


def emit_json(payload: object) -> None:
    """Write a JSON document to stdout for ``--json`` command output."""
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


# Official GitHub CLI install page (all platforms).
GH_INSTALL_URL = "https://cli.github.com/"


def github_cli_status() -> tuple[str, str]:
    """Probe the GitHub CLI for GitHub-backed features (inbox sync, repo status).

    Returns ``(status, detail)`` where status is ``"ok"`` (installed and
    authenticated), or ``"warn"`` (missing, or present but not logged in) with a
    detail that points at the official install page / `gh auth login`."""
    import shutil
    import subprocess

    if shutil.which("gh") is None:
        return "warn", f"GitHub CLI (`gh`) not found — install it from {GH_INSTALL_URL}, then run `gh auth login`."
    try:
        proc = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, check=False, timeout=15
        )
    except Exception as exc:  # timeout / OS error — treat as unusable
        return "warn", f"GitHub CLI is installed but `gh auth status` failed: {exc}"
    if proc.returncode == 0:
        return "ok", "GitHub CLI is installed and authenticated."
    return "warn", "GitHub CLI is installed but not authenticated. Run `gh auth login`."


def agent_author(default: str | None = None) -> str | None:
    """The author to stamp on agent-authored writes, from the run environment.

    The orchestrator exports ``ARCHON_HORIZON_AGENT_ROLE`` (``ground``/``horizon``)
    for the agent it is running; everything else falls back to ``default``.
    """
    role = os.environ.get("ARCHON_HORIZON_AGENT_ROLE", "").strip().lower()
    if role in {"ground", "horizon"}:
        return role
    return default


def refuse_agents(action: str) -> None:
    """Block the ground/horizon agent from a human-only task action.

    Tasks are the human's lever for launching sessions; agents organize pending
    work through the roadmap, never by authoring tasks. Agents may still read and
    ``comment`` on tasks — neither affects what the orchestrator runs.
    """
    role = os.environ.get("ARCHON_HORIZON_AGENT_ROLE", "").strip().lower()
    if role in {"ground", "horizon"}:
        import typer

        from archon_horizon.log import log

        log.error(
            f"The {role} agent may not {action}: tasks are human-only. Organize pending work "
            "through the roadmap (`horizon roadmap …`); you can still `horizon task comment`."
        )
        raise typer.Exit(2)


def agent_provenance() -> dict | None:
    """Structured provenance for an agent-authored write, from the run env.

    The orchestrator exports ``ARCHON_HORIZON_RUN`` / ``ARCHON_HORIZON_SESSION``
    (and ``ARCHON_HORIZON_AGENT_ROLE``) for the agent it runs; a native subagent
    may additionally export ``ARCHON_HORIZON_SUBAGENT``. Returns ``None`` outside
    a run (e.g. a human at the CLI), so nothing is stamped.
    """
    fields = {
        "run": os.environ.get("ARCHON_HORIZON_RUN", "").strip(),
        "session": os.environ.get("ARCHON_HORIZON_SESSION", "").strip(),
        "role": os.environ.get("ARCHON_HORIZON_AGENT_ROLE", "").strip().lower(),
        "subagent": os.environ.get("ARCHON_HORIZON_SUBAGENT", "").strip(),
    }
    prov = {k: v for k, v in fields.items() if v}
    return prov or None


def with_provenance(metadata: dict | None = None) -> dict:
    """Merge the run-env provenance into ``metadata`` (no-op outside a run)."""
    merged = dict(metadata or {})
    prov = agent_provenance()
    if prov:
        merged["provenance"] = prov
    return merged


def history_entry(actor: str | None, field: str, *, before: str = "", after: str = "", note: str = "") -> dict:
    """Build one append-only history transition for roadmap/task/inbox items."""
    from archon_horizon.core.clock import utc_now

    return {
        "at": utc_now().isoformat(),
        "actor": (actor or "").strip() or "system",
        "field": field,
        "from": before,
        "to": after,
        "note": note,
    }


def load_workspace(root: Path):
    cfg = load_config(root)
    workspace = build_workspace(cfg, root)
    # Best-effort: warn once if the workspace was built by a different Horizon.
    from archon_horizon.core.version import warn_on_drift

    warn_on_drift(root)
    return cfg, workspace


def local_inbox(workspace) -> FilesystemInboxProvider:
    return FilesystemInboxProvider(workspace.state_path / "inbox" / "local")


def roadmap_store(workspace):
    """The roadmap store, using per-item YAML shards."""
    from archon_horizon.store.codec import YamlCodec
    from archon_horizon.store.filesystem import FilesystemRoadmapStore

    codec = YamlCodec()
    return FilesystemRoadmapStore(workspace.state_path / "roadmap", codec)


def task_store(workspace):
    """The task store, using the YAML codec — every write goes through
    ``yaml.safe_dump``, so editing tasks via the CLI can't corrupt them."""
    from archon_horizon.store.codec import YamlCodec
    from archon_horizon.store.filesystem import FilesystemTaskStore

    return FilesystemTaskStore(workspace.state_path / "tasks", YamlCodec())


def inbox_providers(cfg, workspace):
    local = local_inbox(workspace)
    providers = [local]
    if cfg.github.enabled and cfg.github.repo:
        providers.append(
            GithubInboxProvider(
                cfg.github.repo,
                workspace.state_path / "inbox" / "github",
                import_policy=cfg.github.import_policy,
            )
        )
    return local, providers
