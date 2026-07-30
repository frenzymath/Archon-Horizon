"""Export a static, github.io-ready copy of the dashboard.

The same React SPA runs live (talking to the Python server) or static (no
server). In static mode the SPA's ``staticMode.ts`` rewrites every
``fetch('/api/<path>')`` to ``./data/api/<sha256(path)>.json``. This exporter
writes exactly those files — the hash MUST match the browser's
``crypto.subtle.digest('SHA-256', ...)``, which is plain
``hashlib.sha256(path).hexdigest()`` over the same path string.

With a built SPA (``dist_dir``) it lays the app down and injects the
``window.__ARCHON_STATIC__`` marker that flips it into static mode. Without
one, the data files are still exported and the index page explains how to
build the SPA (there is no server-rendered fallback view).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from archon_horizon.server.service import WorkspaceService



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
      url: ${{ steps.deploy-1.outputs.page_url || steps.deploy-2.outputs.page_url || steps.deploy-3.outputs.page_url }}
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Configure Pages
        uses: actions/configure-pages@v5

      # Upload the already-built dashboard exactly once. The artifact is always
      # named "github-pages"; a manual re-run would upload a SECOND artifact onto
      # the same run and make deploy-pages fail with "Multiple artifacts named
      # github-pages". Don't re-run this workflow -- the deploy step below already
      # retries transient failures, and the next push redeploys anyway.
      - name: Upload dashboard
        uses: actions/upload-pages-artifact@v3
        with:
          path: __UPLOAD_PATH__

      # GitHub's Pages backend intermittently returns "Deployment failed, try
      # again later." for a perfectly valid upload while Pages is fully
      # operational. Retry up to three times within the same run so a transient
      # blip doesn't leave the site stale until the next push. Only the last
      # attempt is allowed to fail the job.
      - name: Deploy to GitHub Pages
        id: deploy-1
        uses: actions/deploy-pages@v4
        continue-on-error: true

      - name: Wait before deploy retry 2
        if: steps.deploy-1.outcome == 'failure'
        run: sleep 45
      - name: Deploy to GitHub Pages (retry 2)
        id: deploy-2
        if: steps.deploy-1.outcome == 'failure'
        uses: actions/deploy-pages@v4
        continue-on-error: true

      - name: Wait before deploy retry 3
        if: steps.deploy-1.outcome == 'failure' && steps.deploy-2.outcome == 'failure'
        run: sleep 90
      - name: Deploy to GitHub Pages (retry 3)
        id: deploy-3
        if: steps.deploy-1.outcome == 'failure' && steps.deploy-2.outcome == 'failure'
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
    exported: list[str] = []
    for path in paths:
        # A handler raises KeyError to mean "404" (e.g. /api/commit?sha=... for a
        # SHA that resolves in no project's git log — cited SHAs in inbox bodies,
        # reports and roadmap pins routinely age out of those logs). The live
        # server turns that into a 404 and the client renders the reference
        # plain; here it would abort the whole export, so skip the endpoint and
        # let the client take the same absent-data path.
        try:
            payload = service.serve_endpoint(path)
        except KeyError:
            continue
        (data_dir / f"{endpoint_key(path)}.json").write_text(
            json.dumps(payload), "utf-8"
        )
        exported.append(path)
    paths = exported
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
        # No built SPA to copy: the data files above are still exported, but
        # there is nothing to render them. Say how to fix it rather than
        # shipping a second, drift-prone server-rendered view.
        (out_dir / "index.html").write_text(
            "<!doctype html><meta charset='utf-8'><title>Archon Horizon</title>"
            "<body style='font-family:system-ui;max-width:640px;margin:80px auto'>"
            "<h1>Static export incomplete</h1>"
            "<p>No built <code>frontend/dist</code> was available when this export ran. "
            "Build it (<code>cd src/archon_horizon/frontend && npm install && npm run build</code>) "
            "and re-run <code>horizon dashboard --static</code>.</p></body>",
            "utf-8",
        )
    return out_dir
