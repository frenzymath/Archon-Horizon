"""Typer-decorated dashboard and server entry points."""

from __future__ import annotations

from pathlib import Path

import typer

from archon_horizon.log import log


def _packaged_dist() -> Path | None:
    candidate = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    return candidate if (candidate / "index.html").exists() else None


class DashboardCommand:
    def __init__(self, root: Path, *, out: str = "dashboard", dist: str | None = None) -> None:
        self.root = root
        self.out = out
        self.dist = dist

    def run(self) -> None:
        from archon_horizon.render.static_export import export_static
        from archon_horizon.server.service import WorkspaceService

        service = WorkspaceService(self.root)
        dist = Path(self.dist) if self.dist else _packaged_dist()
        out = export_static(service, self.root / self.out, dist_dir=dist)
        note = "" if dist else " (Python fallback; build the SPA and pass --dist for the full app)"
        log.success(f"exported static dashboard to {out / 'index.html'}{note}")


class ServeCommand:
    def __init__(
        self,
        root: Path,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        dist: str | None = None,
    ) -> None:
        self.root = root
        self.host = host
        self.port = port
        self.dist = dist

    def run(self) -> None:
        from archon_horizon.server.app import serve

        dist = Path(self.dist) if self.dist else _packaged_dist()
        serve(self.root, host=self.host, port=self.port, dist_dir=dist)


def dashboard(
    ctx: typer.Context,
    out: str = typer.Option("dashboard", "--out", help="Output directory."),
    dist: str | None = typer.Option(None, "--dist", help="Built SPA dir, e.g. frontend/dist."),
) -> None:
    """Export a static dashboard snapshot."""
    DashboardCommand(ctx.obj["root"], out=out, dist=dist).run()


def serve(
    ctx: typer.Context,
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind."),
    port: int = typer.Option(8765, "--port", help="Port to bind."),
    dist: str | None = typer.Option(None, "--dist", help="Built SPA dir, e.g. frontend/dist."),
) -> None:
    """Run the live dashboard server."""
    ServeCommand(ctx.obj["root"], host=host, port=port, dist=dist).run()

