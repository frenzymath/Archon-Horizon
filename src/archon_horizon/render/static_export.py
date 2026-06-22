"""Export a static, github.io-ready copy of the dashboard.

The same React SPA runs live (talking to the Python server) or static (no
server). In static mode the SPA's ``staticMode.ts`` rewrites every
``fetch('/api/<path>')`` to ``./data/api/<sha256(path)>.json``. This exporter
writes exactly those files — the hash MUST match the browser's
``crypto.subtle.digest('SHA-256', ...)``, which is plain
``hashlib.sha256(path).hexdigest()`` over the same path string.

With a built SPA (``dist_dir``) it lays the app down and injects the
``window.__ARCHON_STATIC__`` marker that flips it into static mode. Without
one, it falls back to the single-file Python-rendered dashboard so a static
page exists even before anyone runs a frontend build.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from archon_horizon.server.service import WorkspaceService

from .dashboard import render_dashboard


def endpoint_key(path: str) -> str:
    """sha256 hex of the API path — must match staticMode.ts."""
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def _inject_static_marker(index_html: str, marker: dict[str, object]) -> str:
    script = f"<script>window.__ARCHON_STATIC__ = {json.dumps(marker)};</script>"
    if "</head>" in index_html:
        return index_html.replace("</head>", f"{script}</head>", 1)
    return script + index_html


def export_static(service: WorkspaceService, out_dir: Path, *, dist_dir: Path | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Dump one JSON file per endpoint, keyed by sha256(path).
    data_dir = out_dir / "data" / "api"
    data_dir.mkdir(parents=True, exist_ok=True)
    paths = service.endpoints()
    for path in paths:
        (data_dir / f"{endpoint_key(path)}.json").write_text(
            json.dumps(service.serve_endpoint(path)), "utf-8"
        )

    marker = {"generatedAt": "", "endpointCount": len(paths)}

    # 2. Lay down the app shell.
    if dist_dir is not None and (dist_dir / "index.html").exists():
        for item in dist_dir.iterdir():
            target = out_dir / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
        index = out_dir / "index.html"
        index.write_text(_inject_static_marker(index.read_text("utf-8"), marker), "utf-8")
    else:
        # Fallback: the single-file Python dashboard (no live API, read-only).
        (out_dir / "index.html").write_text(
            render_dashboard(
                workspace_name=service.workspace.name,
                roadmap=service.stores.roadmap.load(),
                local_items=service.local.list_items(),
                github_items=service.github.list_items() if service.github else (),
                memory=service.stores.memory.load(),
                reports=service.stores.reports.list(),
            ),
            "utf-8",
        )
    return out_dir
