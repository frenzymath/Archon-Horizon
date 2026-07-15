"""Structural workspace operations.

Per the roadmap, the project set changes only through explicit operations that
update ``config.yaml`` and record events — never as an implicit side effect. A
human or an agent invokes them via the ``horizon project`` CLI; an agent that
restructures projects must keep the rest consistent (config, and any
inbox/roadmap/tasks referencing a renamed or removed project) and inform the
user (e.g. an ``info`` inbox item).

Note: these round-trip ``config.yaml`` through ``yaml.safe_load``/``safe_dump``,
so hand-written comments are not preserved. Keep prose docs elsewhere.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

import yaml

from archon_horizon.core.events import Event
from archon_horizon.store.base import EventLog
from archon_horizon.vcs.git import GitError, WorkspaceGit, git_available, neutralize_nested_git

from .loader import CONFIG_FILENAME


def _load_raw(root: Path) -> dict[str, Any]:
    return yaml.safe_load((root / CONFIG_FILENAME).read_text("utf-8")) or {}


def _save_raw(root: Path, data: dict[str, Any]) -> None:
    (root / CONFIG_FILENAME).write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), "utf-8"
    )


def _emit(event_log: EventLog | None, type: str, **data: Any) -> None:
    if event_log is not None:
        event_log.append(Event(type=type, id=uuid.uuid4().hex, actor="workspace-op", data=data))


def add_project(
    root: Path,
    name: str,
    path: str,
    *,
    type: str = "lean",
    build_command: str | None = None,
    event_log: EventLog | None = None,
) -> None:
    data = _load_raw(root)
    projects = data.setdefault("projects", {})
    if name in projects:
        raise ValueError(f"project {name!r} already exists")
    entry: dict[str, Any] = {
        "path": path,
        "type": type,
    }
    if build_command:
        entry["build"] = {"command": build_command}
    projects[name] = entry
    _save_raw(root, data)
    project_dir = root / path
    project_dir.mkdir(parents=True, exist_ok=True)
    vcs_error = None
    if git_available():
        try:
            # If the project was cloned with its own in-tree .git, rename it aside
            # so its files (not a submodule gitlink) are tracked, then record the
            # new project in the single workspace ledger so its tree is tracked
            # from registration.
            disabled = neutralize_nested_git(project_dir)
            if disabled:
                _emit(event_log, "workspace.project.nested_git_disabled", project=name, path=disabled)
            wsgit = WorkspaceGit(root)
            wsgit.init()
            wsgit.unstage_gitlink(path)
            wsgit.commit(f"workspace: register project {name}", paths=["config.yaml", path])
        except GitError as exc:
            vcs_error = str(exc)
    _emit(event_log, "workspace.project.added", project=name, path=path)
    if vcs_error:
        _emit(event_log, "workspace.project.vcs_init_failed", project=name, error=vcs_error)


def remove_project(root: Path, name: str, *, event_log: EventLog | None = None) -> None:
    data = _load_raw(root)
    if name not in data.get("projects", {}):
        raise ValueError(f"project {name!r} does not exist")
    del data["projects"][name]
    _save_raw(root, data)
    _emit(event_log, "workspace.project.removed", project=name)


def archive_project(root: Path, name: str, *, event_log: EventLog | None = None) -> Path:
    """Drop the project from the active set and move its tree under archive/."""
    data = _load_raw(root)
    projects = data.get("projects", {})
    if name not in projects:
        raise ValueError(f"project {name!r} does not exist")
    entry = projects.pop(name)
    src = root / entry["path"]
    dest = root / data.get("workspace", {}).get("state_dir", ".archon-horizon") / "archive" / name
    if src.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    _save_raw(root, data)
    _emit(event_log, "workspace.project.archived", project=name, archived_to=str(dest))
    return dest


def merge_projects(
    root: Path, dest: str, source: str, *, event_log: EventLog | None = None
) -> None:
    """Move ``source``'s files into ``dest`` and drop ``source`` from config."""
    data = _load_raw(root)
    projects = data.get("projects", {})
    for name in (dest, source):
        if name not in projects:
            raise ValueError(f"project {name!r} does not exist")
    dest_dir = root / projects[dest]["path"]
    source_dir = root / projects[source]["path"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    if source_dir.exists():
        for child in source_dir.iterdir():
            target = dest_dir / child.name
            if target.exists():
                raise ValueError(f"merge conflict: {target} already exists")
            shutil.move(str(child), str(target))
        shutil.rmtree(source_dir, ignore_errors=True)
    del projects[source]
    _save_raw(root, data)
    _emit(event_log, "workspace.project.merged", dest=dest, source=source)
