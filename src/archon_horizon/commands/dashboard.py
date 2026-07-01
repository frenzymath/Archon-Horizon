"""Typer-decorated dashboard and server entry points."""

from __future__ import annotations

from pathlib import Path

import typer

from archon_horizon.log import log

from .shared import emit_json


def _packaged_dist() -> Path | None:
    candidate = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    return candidate if (candidate / "index.html").exists() else None


class DashboardCommand:
    def __init__(
        self,
        root: Path,
        *,
        out: str = "dashboard",
        dist: str | None = None,
        as_json: bool = False,
        workflow: bool = False,
    ) -> None:
        self.root = root
        self.out = out
        self.dist = dist
        self.as_json = as_json
        self.workflow = workflow

    def run(self) -> None:
        from archon_horizon.render.static_export import export_static, write_pages_workflow

        from archon_horizon.server.service import WorkspaceService

        service = WorkspaceService(self.root)
        dist = Path(self.dist) if self.dist else _packaged_dist()
        out = export_static(service, self.root / self.out, dist_dir=dist)

        workflow_path: Path | None = None
        workflow_status = ""
        if self.workflow:
            wf, workflow_status = write_pages_workflow(self.root, out)
            workflow_path = wf if workflow_status != "outside" else None

        if self.as_json:
            emit_json({
                "exported": str(out / "index.html"),
                "fallback": dist is None,
                "workflow": str(workflow_path) if workflow_path else None,
                "workflow_status": workflow_status or None,
            })
            return

        log.success(f"exported static dashboard to {out / 'index.html'}")
        if dist is None:
            # The single-file fallback renders roadmap/inbox/blueprint/memory/reports
            # but NOT the run logs (transcript viewer). Build the SPA for the full
            # dashboard, including logs.
            log.warn(
                "This is the read-only Python fallback: it does not include the run "
                "logs / transcript viewer. Build the SPA (`npm --prefix "
                "frontend install && npm --prefix frontend run build`) and re-run "
                "with `--dist frontend/dist` for the full dashboard with logs."
            )
        if self.workflow:
            self._report_workflow(out, workflow_path, workflow_status)

    def _report_workflow(self, out: Path, workflow_path: Path | None, status: str) -> None:
        if status == "outside":
            log.warn(
                f"--out {out} is not inside the workspace repo, so no Pages workflow was "
                "written; choose an --out under the repository root."
            )
            return
        if status == "exists":
            log.info(f"GitHub Pages workflow already present at {workflow_path}; left untouched.")
        else:
            log.success(f"GitHub Pages workflow written to {workflow_path}")
        rel = out.relative_to(self.root).as_posix() if out.is_relative_to(self.root) else out
        log.step(
            f"Commit `{rel}/` and the workflow, push to main, then enable Pages "
            "(Settings → Pages → Source: GitHub Actions)."
        )


class ServeCommand:
    def __init__(
        self,
        root: Path,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        dist: str | None = None,
        as_json: bool = False,
    ) -> None:
        self.root = root
        self.host = host
        self.port = port
        self.dist = dist
        self.as_json = as_json

    def run(self) -> None:
        from archon_horizon.server.app import create_server_with_fallback, serve, serve_server

        dist = Path(self.dist) if self.dist else _packaged_dist()
        if self.as_json:
            server = create_server_with_fallback(self.root, self.host, self.port, dist)
            actual_port = server.server_address[1]
            emit_json({"serving": {"host": self.host, "port": actual_port, "url": f"http://{self.host}:{actual_port}"}})
            serve_server(server, root=self.root, host=self.host, requested_port=self.port, dist_dir=dist)
            return
        serve(self.root, host=self.host, port=self.port, dist_dir=dist)


def dashboard(
    ctx: typer.Context,
    static: bool = typer.Option(
        False, "--static", help="Export a static snapshot instead of running the live server."
    ),
    out: str = typer.Option("dashboard", "--out", help="Output directory for the --static export."),
    workflow: bool = typer.Option(
        False, "--workflow",
        help="With --static, also write a GitHub Pages deploy workflow (.github/workflows/) "
             "that publishes the exported dashboard.",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind (live server)."),
    port: int = typer.Option(8765, "--port", help="Port to bind (live server)."),
    dist: str | None = typer.Option(None, "--dist", help="Built SPA dir, e.g. frontend/dist."),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout."),
) -> None:
    """Run the live dashboard server, or export a static snapshot with --static."""
    if static:
        DashboardCommand(ctx.obj["root"], out=out, dist=dist, as_json=as_json, workflow=workflow).run()
    elif workflow:
        raise typer.BadParameter("--workflow only applies with --static.")
    else:
        ServeCommand(ctx.obj["root"], host=host, port=port, dist=dist, as_json=as_json).run()
