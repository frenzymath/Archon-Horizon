"""Shared command helpers."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from archon_horizon.config.loader import build_workspace, load_config
from archon_horizon.core.provenance import agent_provenance
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

    The orchestrator exports ``ARCHON_HORIZON_AGENT_ROLE`` (``horizon``) for the
    agent it is running; everything else falls back to ``default``.
    """
    role = os.environ.get("ARCHON_HORIZON_AGENT_ROLE", "").strip().lower()
    if role == "horizon":
        return role
    return default


def ensure_concise_agent_message(
    body: str,
    noun: str,
    *,
    author: str | None = None,
    soft_limit: int = 600,
    hard_limit: int = 1200,
) -> None:
    """Bound operational prose written by agents while leaving humans free-form.

    Normal updates fit below ``soft_limit``. A genuinely detailed finding may
    exceed it when structured as Markdown, but operational state is not a report
    archive: beyond ``hard_limit`` the detail belongs in a commit summary,
    report, source file, or a narrowly scoped attachment.
    """
    # A running agent cannot bypass the bound by passing `--author human`.
    # Outside a run, explicit human authorship remains unrestricted.
    if agent_author() != "horizon" and author != "horizon":
        return
    text = str(body or "").strip()
    if len(text) > hard_limit:
        raise ValueError(
            f"agent {noun} is too long ({len(text)} characters; maximum {hard_limit}); "
            "record only the conclusion, evidence delta, and next action here"
        )
    if len(text) <= soft_limit:
        return
    structured = re.search(
        r"\n\s*\n|(?:^|\n)\s*(?:#{1,6}\s+|[-*+]\s+|\d+\.\s+|>\s*|```|\|)",
        text,
    )
    if not structured:
        raise ValueError(
            f"long agent {noun} needs scannable Markdown; use short paragraphs "
            "or at most three concise bullets"
        )


def with_provenance(metadata: dict | None = None) -> dict:
    """Merge the run-env provenance into ``metadata`` (no-op outside a run)."""
    merged = dict(metadata or {})
    prov = agent_provenance()
    if prov:
        merged["provenance"] = prov
    return merged


def provenance_task(default: str | None = None) -> str | None:
    """The task id of the running session, if any (``ARCHON_HORIZON_TASK``)."""
    return os.environ.get("ARCHON_HORIZON_TASK", "").strip() or default


def provenance_project(default: str | None = None) -> str | None:
    """The first project of the running session (``ARCHON_HORIZON_PROJECTS``).

    Used to default ``--project`` so an agent rarely needs to pass it. A comma or
    space separated list keeps only the first — the session's primary project.
    """
    raw = os.environ.get("ARCHON_HORIZON_PROJECTS", "").strip()
    if not raw:
        return default
    first = raw.replace(",", " ").split()
    return first[0] if first else default


def reader_id() -> str:
    """Stable identity of who is reading, for inbox read-state.

    A team is a task, so prefer the task id; fall back to the run, then the agent
    role, then ``"human"`` for a person at the CLI. This is the id recorded in an
    item's ``read_by`` list and used to compute "unread for me".
    """
    for var in ("ARCHON_HORIZON_TASK", "ARCHON_HORIZON_RUN"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    role = os.environ.get("ARCHON_HORIZON_AGENT_ROLE", "").strip().lower()
    return role or "human"


def conversation_sender() -> str:
    """Canonical route target for the participant starting a conversation."""
    task = os.environ.get("ARCHON_HORIZON_TASK", "").strip()
    if task:
        return f"task:{task}"
    run = os.environ.get("ARCHON_HORIZON_RUN", "").strip()
    if run:
        return f"run:{run}"
    role = os.environ.get("ARCHON_HORIZON_AGENT_ROLE", "").strip().lower()
    return role or "human"


def history_entry(actor: str | None, field: str, *, before: str = "", after: str = "", note: str = "") -> dict:
    """Build one append-only history transition for roadmap/task/inbox items.

    When the write happens inside an agent session and the actor is that session's
    role, the run/session provenance is stamped onto the entry — mirroring the
    inbox provider's own history recorder. This is what lets the dashboard
    attribute a roadmap/task status change to the session that made it (the
    per-session "Roadmap activity" feed), rather than only creations and comments."""
    from archon_horizon.core.clock import utc_now
    from archon_horizon.core.provenance import agent_provenance

    actor_name = (actor or "").strip() or "system"
    entry: dict[str, object] = {
        "at": utc_now().isoformat(),
        "actor": actor_name,
        "field": field,
        "from": before,
        "to": after,
        "note": note,
    }
    provenance = agent_provenance()
    if provenance and actor_name.lower() == provenance.get("role"):
        entry["provenance"] = provenance
    return entry


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
    """The roadmap store — same construction as the orchestrator's (build_stores),
    so the CLI and the run loop can never drift on codec/paths."""
    from archon_horizon.config.loader import build_stores

    return build_stores(workspace).roadmap


def task_store(workspace):
    """The task store — same construction as the orchestrator's (build_stores)."""
    from archon_horizon.config.loader import build_stores

    return build_stores(workspace).tasks


def task_inbox_refs(workspace, task_id: str | None) -> tuple[str, ...]:
    """Inbox items explicitly linked to ``task_id``, or none for unknown tasks."""
    if not task_id:
        return ()
    try:
        return task_store(workspace).get(task_id).inbox_refs
    except (FileNotFoundError, KeyError):
        return ()


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
