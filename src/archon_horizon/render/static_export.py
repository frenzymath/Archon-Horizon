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


# The exported dashboard is committed to the workspace repo; this Action just
# uploads that already-built directory and deploys it to GitHub Pages. It does
# NOT rebuild in CI, so it needs no Python/Node toolchain and never depends on
# the (gitignored) SPA build being present in a fresh checkout.
PAGES_WORKFLOW_FILENAME = "horizon-dashboard-pages.yml"

_PAGES_WORKFLOW = """name: Deploy Horizon dashboard

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

# Allow one concurrent deployment; don't cancel an in-progress one.
concurrency:
  group: pages
  cancel-in-progress: false

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Configure Pages
        uses: actions/configure-pages@v5

      - name: Upload dashboard
        uses: actions/upload-pages-artifact@v3
        with:
          path: __UPLOAD_PATH__

      - name: Deploy to GitHub Pages
        id: deployment
        uses: actions/deploy-pages@v4
"""


def write_pages_workflow(
    repo_root: Path, out_dir: Path, *, force: bool = False
) -> tuple[Path, str]:
    """Write the GitHub Pages deploy workflow into ``repo_root/.github/workflows``.

    The workflow uploads ``out_dir`` (the exported, committed dashboard) verbatim
    and deploys it. Returns ``(path, status)`` where status is ``"written"``,
    ``"exists"`` (left untouched), or ``"outside"`` (out_dir not under the repo,
    so nothing was written and the returned path is the offending out_dir).
    """
    try:
        upload_path = out_dir.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return out_dir, "outside"
    workflow_path = repo_root / ".github" / "workflows" / PAGES_WORKFLOW_FILENAME
    if workflow_path.exists() and not force:
        return workflow_path, "exists"
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(_PAGES_WORKFLOW.replace("__UPLOAD_PATH__", json.dumps(upload_path)), "utf-8")
    return workflow_path, "written"


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
    reports_src = service.workspace.state_path / "reports"
    if reports_src.exists():
        shutil.copytree(reports_src, out_dir / "reports", dirs_exist_ok=True)

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
