"""Shared command helpers."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.config.loader import build_workspace, load_config
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider
from archon_horizon.inboxes.github import GithubInboxProvider


def load_workspace(root: Path):
    cfg = load_config(root)
    workspace = build_workspace(cfg, root)
    return cfg, workspace


def local_inbox(workspace) -> FilesystemInboxProvider:
    return FilesystemInboxProvider(workspace.state_path / "inboxes" / "local.yaml")


def inbox_providers(cfg, workspace):
    local = local_inbox(workspace)
    providers = [local]
    if cfg.github.enabled and cfg.github.repo:
        providers.append(
            GithubInboxProvider(cfg.github.repo, workspace.state_path / "inboxes" / "github-shadow.yaml")
        )
    return local, providers

