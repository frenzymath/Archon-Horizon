"""Typer-decorated dashboard and server entry points."""

from __future__ import annotations

from pathlib import Path

import typer

from archon_horizon.log import log

from .shared import emit_json

LOCAL_DASHBOARD_HOST = "127.0.0.1"
PUBLIC_DASHBOARD_HOST = "0.0.0.0"


def _packaged_dist() -> Path | None:
    candidate = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    return candidate if (candidate / "index.html").exists() else None


def resolve_dashboard_host(host: str, public: bool = False) -> str:
    if not public:
        return host
    if host not in (LOCAL_DASHBOARD_HOST, PUBLIC_DASHBOARD_HOST):
        raise ValueError("--public cannot be combined with a custom --host.")
    return PUBLIC_DASHBOARD_HOST


class DashboardCommand:
    def __init__(
        self,
        root: Path,
        *,
        out: str = "dashboard",
        dist: str | None = None,
        as_json: bool = False,
        workflow: bool = False,
        history_limit: int = 8,
        transcript_page_limit: int = 2,
    ) -> None:
        self.root = root
        self.out = out
        self.dist = dist
        self.as_json = as_json
        self.workflow = workflow
        self.history_limit = history_limit
        self.transcript_page_limit = transcript_page_limit

    def run(self) -> None:
        from archon_horizon.render.static_export import export_static, write_pages_workflow

        from archon_horizon.server.service import WorkspaceService

        service = WorkspaceService(self.root)
        dist = Path(self.dist) if self.dist else _packaged_dist()
        out = export_static(
            service,
            self.root / self.out,
            dist_dir=dist,
            history_limit=self.history_limit,
            transcript_page_limit=self.transcript_page_limit,
        )

        workflow_path: Path | None = None
        workflow_status = ""
        if self.workflow:
            wf, workflow_status = write_pages_workflow(self.root, out)
            workflow_path = wf if workflow_status != "outside" else None

        commit_sha = self._commit_export(out, workflow_path)

        if self.as_json:
            emit_json({
                "exported": str(out / "index.html"),
                "fallback": dist is None,
                "workflow": str(workflow_path) if workflow_path else None,
                "workflow_status": workflow_status or None,
                "committed": commit_sha,
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
            self._report_workflow(out, workflow_path, workflow_status, committed=commit_sha)

    def _commit_export(self, out: Path, workflow_path: Path | None) -> str | None:
        """Commit the exported dashboard (and Pages workflow) into the workspace
        ledger, so it is actually tracked and can be pushed to publish Pages.

        The static export writes a built site under ``--out`` (default
        ``dashboard/``) plus, with ``--workflow``, a file under
        ``.github/workflows/``. Neither lives under the paths the per-session
        autogit integration stages, so without this step they stay untracked —
        the "static page is gitignored" symptom (they are not ignored, just never
        added). Best-effort: needs git and paths inside the workspace repo; a
        failure warns rather than aborting the export.
        """
        from archon_horizon.vcs.git import GitError, WorkspaceGit, git_available

        if not git_available():
            log.warn("git not available; exported dashboard left uncommitted.")
            return None
        paths: list[str] = []
        for candidate in (out, workflow_path):
            if candidate is None:
                continue
            try:
                paths.append(candidate.resolve().relative_to(self.root.resolve()).as_posix())
            except ValueError:
                # Outside the workspace repo (custom --out); nothing to commit.
                continue
        if not paths:
            return None
        try:
            git = WorkspaceGit(self.root)
            git.init()
            sha = git.commit("workspace: publish static dashboard", paths=paths)
        except GitError as exc:
            log.warn(f"could not commit exported dashboard: {exc}")
            return None
        if sha:
            log.info(f"committed exported dashboard to the workspace ledger ({sha[:10]}).")
        return sha

    def _report_workflow(
        self, out: Path, workflow_path: Path | None, status: str, *, committed: str | None
    ) -> None:
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
        if committed:
            log.step(
                "Push the workspace repo to your GitHub remote, then enable Pages "
                "(Settings → Pages → Source: GitHub Actions)."
            )
        else:
            rel = out.relative_to(self.root).as_posix() if out.is_relative_to(self.root) else out
            log.step(
                f"Commit `{rel}/` and the workflow, push to main, then enable Pages "
                "(Settings → Pages → Source: GitHub Actions)."
            )
        log.info(
            "If the deploy fails with \"Branch main is not allowed to deploy to "
            "github-pages\", clear the environment's branch rule: Settings → "
            "Environments → github-pages → Deployment branches and tags → allow "
            "main (or 'No restriction')."
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
        from archon_horizon.server.app import dashboard_url, create_server_with_fallback, serve, serve_server

        dist = Path(self.dist) if self.dist else _packaged_dist()
        if self.as_json:
            server = create_server_with_fallback(self.root, self.host, self.port, dist)
            actual_port = server.server_address[1]
            emit_json({
                "serving": {
                    "host": self.host,
                    "port": actual_port,
                    "url": dashboard_url(self.host, actual_port),
                }
            })
            serve_server(server, root=self.root, host=self.host, requested_port=self.port, dist_dir=dist)
            return
        serve(self.root, host=self.host, port=self.port, dist_dir=dist)


def dashboard(
    ctx: typer.Context,
    static: bool = typer.Option(
        False, "--static", help="Export a static snapshot instead of running the live server."
    ),
    out: str = typer.Option("dashboard", "--out", help="Output directory for the --static export."),
    history_limit: int = typer.Option(
        8,
        "--history-limit",
        help="Maximum recent sessions to include in a static snapshot (live mode is unaffected).",
    ),
    transcript_page_limit: int = typer.Option(
        2,
        "--transcript-page-limit",
        help="Maximum 120-event pages per session in a static snapshot (live mode is unaffected).",
    ),
    workflow: bool = typer.Option(
        False, "--workflow",
        help="With --static, also write a GitHub Pages deploy workflow (.github/workflows/) "
             "that publishes the exported dashboard.",
    ),
    host: str = typer.Option(LOCAL_DASHBOARD_HOST, "--host", help="Host to bind (live server)."),
    port: int = typer.Option(8765, "--port", help="Port to bind (live server)."),
    public: bool = typer.Option(
        False,
        "--public",
        help="Bind the live server to all IPv4 interfaces (equivalent to --host 0.0.0.0).",
    ),
    dist: str | None = typer.Option(None, "--dist", help="Built SPA dir, e.g. frontend/dist."),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout."),
) -> None:
    """Run the live dashboard server, or export a static snapshot with --static."""
    if static:
        DashboardCommand(
            ctx.obj["root"],
            out=out,
            dist=dist,
            as_json=as_json,
            workflow=workflow,
            history_limit=history_limit,
            transcript_page_limit=transcript_page_limit,
        ).run()
    elif workflow:
        raise typer.BadParameter("--workflow only applies with --static.")
    else:
        try:
            bind_host = resolve_dashboard_host(host, public)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        ServeCommand(ctx.obj["root"], host=bind_host, port=port, dist=dist, as_json=as_json).run()
