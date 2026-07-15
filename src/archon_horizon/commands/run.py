"""Typer-decorated ``run`` entry point."""

from __future__ import annotations

import hashlib
import dataclasses
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import typer

from archon_horizon.config.harnesses import HarnessRegistry
from archon_horizon.config.loader import build_orchestrator
from archon_horizon.core.sessions import Focus, RunRecord
from archon_horizon.core.scope import ItemScope
from archon_horizon.core.tasks import HorizonTask, TaskStatus, WriteSet
from archon_horizon.log import log

from .dashboard import LOCAL_DASHBOARD_HOST, resolve_dashboard_host
from .shared import emit_json, inbox_providers, load_workspace

# The single-agent run targets. ``horizon run ground`` / ``horizon run horizon``
# drive exactly one session of that role instead of the usual G/H alternation.
ROLE_TARGETS = ("ground", "horizon")


class RunCommand:
    def __init__(
        self,
        root: Path,
        *,
        targets: tuple[str, ...] = (),
        task: str | None = None,
        rounds: int | None = None,
        dry_run: bool = False,
        resume: str | None = None,
        backend: str = "default",
        run_id: str | None = None,
        round_index: int | None = None,
        as_json: bool = False,
        dashboard: bool = True,
        dashboard_host: str = LOCAL_DASHBOARD_HOST,
        dashboard_port: int = 8765,
    ) -> None:
        self.root = root
        self.targets = targets
        self.task = task
        self.rounds = rounds
        self.dry_run = dry_run
        self.resume = resume
        self.backend = (backend or "default").strip().lower()
        self.run_id = (run_id or "").strip() or None
        self.round_index = round_index
        self.as_json = as_json
        self.dashboard = dashboard
        self.dashboard_host = dashboard_host
        self.dashboard_port = dashboard_port

    def run(self) -> None:
        if not self.targets and self.task:
            self.targets = (self.task,)

        with self._dashboard_server():
            # `--backend interactive` hands the terminal straight to the engine so a
            # human can drive the agent (type follow-ups). It is a single human-driven
            # session, not the headless orchestrated alternation. The same mode is
            # requested from config by setting `backend: interactive` on a role's
            # harness — resolved here so it behaves like the CLI flag: whatever the
            # target (a task, a project, `.`, or a bare role), if the role that would
            # run declares it, we launch that one role interactively, seeded with the
            # focus, instead of dispatching it headlessly.
            if self.backend != "interactive":
                role = self._config_interactive_role()
                if role is not None:
                    self.backend = "interactive"
                    self._interactive_role = role
            if self.backend == "interactive":
                self._run_interactive()
                return

            cfg, workspace = load_workspace(self.root)
            self._install_native_subagents(cfg, workspace)
            _, providers = inbox_providers(cfg, workspace)
            orch = build_orchestrator(self.root, registry=HarnessRegistry(), inbox_providers=providers)

            if self.resume is not None:
                run = self._resume_run(orch)
                reports = orch.run(run, resume=True, rounds_override=self.rounds)
                self._emit_reports(reports)
                return

            if not self.targets:
                log.error("Specify what to run: `horizon run .`, `horizon run '*'`, `ground`, `horizon`, task names, project names, or files.")
                raise typer.Exit(1)

            # `horizon run ground` / `horizon run horizon`: one session of that role.
            if len(self.targets) == 1 and self.targets[0] in ROLE_TARGETS:
                reports = self._run_single_role(orch, self.targets[0])
                self._emit_reports(reports)
                return

            focus = self._resolve_focus(orch, tuple(self.targets))
            run = RunRecord(id="", focus=focus, rounds_requested=self.rounds or cfg.rounds)
            reports = orch.run(run, dry_run=self.dry_run)
            self._emit_reports(reports)

    @contextmanager
    def _dashboard_server(self) -> Iterator[None]:
        if not self.dashboard:
            yield
            return
        from archon_horizon.commands.dashboard import _packaged_dist
        from archon_horizon.server.app import create_server_with_fallback, serve_server

        dist = _packaged_dist()
        try:
            server = create_server_with_fallback(
                self.root,
                self.dashboard_host,
                self.dashboard_port,
                dist,
            )
        except OSError as exc:
            log.warn(f"Could not start dashboard on {self.dashboard_host}:{self.dashboard_port}: {exc}")
            yield
            return

        thread = threading.Thread(
            target=serve_server,
            kwargs={
                "server": server,
                "root": self.root,
                "host": self.dashboard_host,
                "requested_port": self.dashboard_port,
                "dist_dir": dist,
            },
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            thread.join(timeout=5)

    def _run_single_role(self, orch, role: str):
        """Drive exactly one session of ``role``.

        Ground: run the opening plan only (``rounds=0`` runs the opener, then the
        loop body never executes). Horizon: skip the opening and closing Ground
        (``start_with``/``end_with`` = ``horizon``) and run a single round, so the
        run is one bare Horizon step over the current focus.

        ``--run <id>`` appends the session to an existing (or new) run directory
        instead of allocating a fresh run, and ``--round <n>`` numbers it — so a
        human hand-driving ground → horizon → horizon → … into one run keeps the
        logs, session metadata, and commit trailers consistent with the automatic
        alternation (and the dashboard groups them under that one run)."""
        run_id = self.run_id or ""
        start_round = self.round_index or 0
        if role == "ground":
            orch.start_with, orch.end_with = "ground", "ground"
            run = RunRecord(id=run_id, focus=Focus(), rounds_requested=0, start_round=start_round)
        else:  # horizon
            orch.start_with, orch.end_with = "horizon", "horizon"
            focus = Focus() if not self.targets[1:] else self._resolve_focus(orch, self.targets[1:])
            run = RunRecord(id=run_id, focus=focus, rounds_requested=1, start_round=start_round)
        return orch.run(run, dry_run=self.dry_run)

    def _config_interactive_role(self) -> str | None:
        """The role to launch interactively when its harness declares
        ``backend: interactive``, else ``None``.

        Interactive is a single human-driven session, so we pick the ONE role the
        target would drive: ``horizon run ground`` → Ground; anything that dispatches
        Horizon (a task, a project, ``.``/``*``, or ``horizon run horizon``) → Horizon
        if the Horizon harness opts in. A ``--resume`` counts too: it continues the
        interrupted run's role interactively (resuming the engine conversation)."""
        try:
            cfg, _ = load_workspace(self.root)
        except Exception:
            return None

        def declares_interactive(name: str | None) -> bool:
            harness = cfg.harnesses.get(name) if name else None
            return harness is not None and str(
                harness.options.get("backend") or ""
            ).strip().lower() == "interactive"

        # An explicit `horizon run ground` is the only way to drive Ground alone.
        if self.targets == ("ground",):
            return "ground" if declares_interactive(cfg.ground_harness) else None
        # Every other target shape ends up running a Horizon step, so the Horizon
        # harness's opt-in governs — seeded with whatever focus was requested.
        return "horizon" if declares_interactive(cfg.horizon_harness) else None

    def _recover_interactive_resume(self, role: str) -> tuple[str | None, tuple[str, ...]]:
        """For an interactive ``--resume``, find the run's last ``role`` session and
        return ``(engine_session_id, focus)`` — the id to hand ``claude --resume`` and
        the task to re-seed. Either may be empty when the run recorded neither."""
        from archon_horizon.config.loader import build_stores

        run_id = (self.resume or "").strip()
        try:
            _, workspace = load_workspace(self.root)
            run_logs = build_stores(workspace).run_logs
            if run_id.lower() in ("", "latest", "last"):
                ids = run_logs.ids()
                run_id = ids[-1] if ids else ""
            elif run_id.isdigit():
                run_id = f"{int(run_id):04d}"  # normalize to the width-4 on-disk id
            if not run_id:
                return None, ()
            sessions = run_logs.get(run_id).sessions()
        except Exception:
            return None, ()
        # Walk newest-first for the last session of this role that pinned an engine id.
        for session in reversed(sessions):
            try:
                meta = session.read_meta()
            except Exception:
                continue
            if meta.get("role") != role:
                continue
            sid = meta.get("engine_session_id")
            task_id = str(meta.get("task_id") or "")
            focus = (task_id,) if task_id else ()
            if isinstance(sid, str) and sid:
                return sid, focus
            if focus:
                # No engine id (older/interrupted-before-first-turn), but we at least
                # recovered the task — a fresh session seeded with it is still useful.
                return None, focus
        return None, ()

    def _run_interactive(self) -> None:
        """Launch the role's harness as an interactive TTY session, seeded with a
        role prompt the human can then steer.

        Unlike a raw TTY launch, this tails the engine's own on-disk session file
        into a Horizon run session as it grows, so a human-driven interactive
        session still shows up in the Log/dashboard (parsed like a headless run) and
        stays ``--resume``-able via the recorded engine session id."""
        from archon_horizon.config.loader import build_stores
        from archon_horizon.core.clock import utc_now

        from .interactive import (
            interactive_launch_for_role,
            interactive_role_prompt,
            run_interactive,
            run_interactive_captured,
        )

        # `focus` = the non-role targets (task ids / projects / files) the human
        # asked for; the interactive session is seeded to start there.
        focus = tuple(t for t in self.targets if t not in ROLE_TARGETS)
        # Prefer the role picked by config routing (`_config_interactive_role`); the
        # plain `--backend interactive` CLI path falls back to the target shape.
        role = getattr(self, "_interactive_role", None)
        if role is None:
            if self.targets and self.targets[0] in ROLE_TARGETS:
                role = self.targets[0]
            elif focus:
                role = "horizon"  # a task/project/file focus is Horizon work
            else:
                role = "ground"
                log.info("`--backend interactive` with no target defaults to the "
                         "ground role; pass `horizon run horizon` or a task/project "
                         "to drive Horizon instead.")

        # `--resume` interactively continues the interrupted run's engine
        # conversation: recover its last matching session's engine id (for a true
        # `claude --resume`) and the task it was on (to seed the focus).
        resume_session_id: str | None = None
        if self.resume is not None:
            resume_session_id, resume_focus = self._recover_interactive_resume(role)
            if resume_focus and not focus:
                focus = resume_focus
            if resume_session_id is None:
                log.info("No resumable engine session found for that run; starting a "
                         "fresh interactive session seeded with its focus instead.")

        prompt = interactive_role_prompt(
            self.root.resolve(), role, focus=focus, resuming=self.resume is not None
        )
        try:
            launch = interactive_launch_for_role(
                self.root, role, prompt, resume_session_id=resume_session_id
            )
        except Exception as exc:
            log.error(f"Could not launch an interactive {role} session: {exc}")
            raise typer.Exit(1)
        if launch is None:
            log.error(f"The {role} harness is 'null'; nothing to launch interactively.")
            raise typer.Exit(1)
        log.info(f"Launching an interactive {role} session using {launch.description}.")

        # A generic engine has no parseable session file, so just hand over the TTY.
        if launch.engine == "generic":
            run_interactive(launch, self.root)
            return

        cfg, workspace = load_workspace(self.root)
        stores = build_stores(workspace)
        runlog = stores.run_logs.allocate()
        run = RunRecord(id=runlog.id, focus=Focus(), rounds_requested=1)
        try:
            stores.runs.put(run)
        except Exception:
            pass
        session = runlog.new_session(f"{role}-interactive")
        base_meta = {
            "role": role,
            "interactive": True,
            "engine": launch.engine,
            "engine_session_id": launch.session_id,
            "started_at": utc_now().isoformat(),
        }
        session.write_meta({**base_meta, "status": "running"})
        log.info(f"Recording this interactive session under run {runlog.id} — visible in the dashboard/Log.")

        returncode = run_interactive_captured(
            launch, self.root, transcript_path=session.transcript_path, role=role, seed_prompt=prompt,
        )

        session.write_meta({
            **base_meta,
            "status": "ok" if returncode == 0 else "failed",
            "ended_at": utc_now().isoformat(),
            "returncode": returncode,
        })
        (session.path / "report.md").write_text(
            f"# Interactive {role} session\n\n"
            f"A human-driven interactive session (engine: `{launch.engine}`). Its conversation was "
            f"mirrored into this run's transcript as it happened. Exit code {returncode}.\n",
            "utf-8",
        )

    def _install_native_subagents(self, cfg, workspace) -> None:
        """Compile descriptors into each engine's workspace-local native agents.

        Done at run start so a change to the descriptors or a harness's tier map
        takes effect on the next run, with no separate install step.
        """
        try:
            from archon_horizon.subagents.compile import install_subagents

            install_subagents(workspace.root, workspace.state_path / "subagents", cfg.harnesses)
        except Exception as exc:  # never block a run on optional subagent compile
            log.warn(f"Skipped subagent compilation: {exc}")

    def _resume_run(self, orch) -> RunRecord:
        """Reconstruct the run to resume: the named run id, else the latest one."""
        run_id = (self.resume or "").strip()
        if run_id.lower() in ("", "latest", "last"):
            ids = orch.run_logs.ids() if orch.run_logs is not None else []
            if not ids:
                log.error("Nothing to resume: this workspace has no recorded runs.")
                raise typer.Exit(1)
            run_id = ids[-1]
        elif run_id.isdigit():
            # Run ids are zero-padded width-4 dir names (see runlog._claim); a bare
            # `--resume 3` must be normalized to `0003` so both the store lookup and
            # the display below match the on-disk id.
            run_id = f"{int(run_id):04d}"
        try:
            run = orch.run_store.get(run_id)
        except Exception:
            log.error(f"Cannot resume run {run_id!r}: no run record found.")
            raise typer.Exit(1)
        # Neutral wording: the orchestrator decides from on-disk sessions whether a
        # round was actually interrupted (recover it) or the run already finished
        # cleanly (run a fresh batch), and its `run.started` banner reports which.
        log.step(f"Resuming run {run_id}.")
        return run

    def _emit_reports(self, reports) -> None:
        rows: list[tuple[str, str, str]] = []
        summary: list[dict] = []
        for report in reports:
            if self.dry_run:
                summary.append({"round": report.round_index, "planned": list(report.planned)})
                rows.append((f"round {report.round_index}", "planned", ", ".join(report.planned) or "-"))
            else:
                summary.append({
                    "round": report.round_index,
                    "tasks_run": list(report.tasks_run),
                    "tasks_blocked": list(report.tasks_blocked),
                })
                detail = f"ran {', '.join(report.tasks_run) or '-'}; blocked {', '.join(report.tasks_blocked) or '-'}"
                rows.append((f"round {report.round_index}", "done", detail))
        if self.as_json:
            emit_json({"dry_run": self.dry_run, "rounds": summary})
            return
        log.results_table(rows, title="Run")

    def _guard_task_status(self, task: HorizonTask) -> None:
        """Stop before running an explicitly-named task whose status says not to.

        ``done`` is terminal — the agent declared the work fully complete, so we do
        not silently reopen it; the human re-queues it deliberately. ``running``
        means another session is already driving it, so starting a second would
        double-run the same task. Either way we print the one command that unblocks
        the situation and exit; every other status (queued/blocked/failed) runs."""
        if task.status is TaskStatus.DONE:
            log.warn(
                f"Task {task.id} is marked done — not starting it. If you want to run it "
                f"again, re-queue it first:\n    horizon task set {task.id} --status queued"
            )
            raise typer.Exit(0)
        if task.status is TaskStatus.RUNNING:
            log.warn(
                f"Task {task.id} is detected as running in another session — not starting a "
                f"second one. If it is actually stuck and you want to run it here, re-queue "
                f"it first:\n    horizon task set {task.id} --status queued"
            )
            raise typer.Exit(0)

    def _resolve_focus(self, orch, targets: tuple[str, ...]) -> Focus:
        if not targets:
            return Focus()
        if "*" in targets:
            if len(targets) != 1:
                log.error("`*` must be the only run target.")
                raise typer.Exit(1)
            return Focus()

        # Resolution order for each target: an existing task id wins, then a
        # roadmap item id (materialized into a task on demand), then a project /
        # file / '.' (an ad-hoc task). This lets `horizon run <roadmap_id>` launch
        # a milestone without the roadmap ever auto-creating tasks.
        known_tasks = {task.id: task for task in orch.task_store.list()}
        roadmap_ids = {item.id for item in orch.roadmap_store.load().items}
        selected: list[str] = []
        remaining: list[str] = []
        for target in targets:
            if target in known_tasks:
                self._guard_task_status(known_tasks[target])
                selected.append(target)
            elif target in roadmap_ids:
                task = orch.ensure_roadmap_task(target)
                if task is None:
                    log.error(f"Roadmap item {target!r} has no project to run in; add one to its scope.")
                    raise typer.Exit(1)
                selected.append(task.id)
            else:
                remaining.append(target)

        if remaining:
            selected.append(self._ensure_ad_hoc_task(orch, tuple(remaining)).id)
        return Focus(tasks=tuple(dict.fromkeys(selected)))

    def _ensure_ad_hoc_task(self, orch, targets: tuple[str, ...]) -> HorizonTask:
        workspace = orch.workspace
        projects: set[str] = set()
        files: list[str] = []

        if targets == (".",):
            projects.update(workspace.projects)
            name = "workspace-all"
            title = "Workspace-wide task"
        else:
            for target in targets:
                if target in workspace.projects:
                    projects.add(target)
                    continue
                rel = Path(target)
                matched = self._project_for_path(workspace, rel)
                if matched is None:
                    log.error(f"Unknown run target {target!r}; expected a task name, project name, '.', '*', or project file path.")
                    raise typer.Exit(1)
                projects.add(matched)
                files.append(rel.as_posix())
            digest = hashlib.sha1("\n".join(targets).encode("utf-8")).hexdigest()[:10]
            name = f"adhoc-{digest}"
            title = "Ad hoc task"

        if not projects:
            log.error("No projects are configured, so no ad hoc task can be inferred.")
            raise typer.Exit(1)
        project_tuple = tuple(sorted(projects))
        task = HorizonTask(
            id=name,
            project=project_tuple[0],
            objective="",
            title=title,
            explanation="",
            projects=project_tuple,
            priority="normal",
            status=TaskStatus.QUEUED,
            write_set=WriteSet(projects=project_tuple, files=tuple(files)),
            scope=ItemScope(projects=project_tuple, files=tuple(files)),
            metadata={"ad_hoc": True, "run_targets": targets},
        )
        existing = {t.id: t for t in orch.task_store.list()}
        if name in existing:
            old = existing[name]
            task = dataclasses.replace(task, created_at=old.created_at)
        return orch.task_store.put(task)

    def _project_for_path(self, workspace, rel: Path) -> str | None:
        raw = rel.as_posix()
        for name, project in workspace.projects.items():
            prefix = project.path.as_posix().rstrip("/") + "/"
            if raw == project.path.as_posix() or raw.startswith(prefix):
                return name
        absolute = (self.root / rel).resolve()
        for name in workspace.projects:
            root = workspace.project_path(name).resolve()
            if absolute == root or root in absolute.parents:
                return name
        return None


def run(
    ctx: typer.Context,
    targets: list[str] = typer.Argument(None, help="Run target: '.', '*', 'ground', 'horizon', a task id, a roadmap item id, project names, or files (resolved task > roadmap > project)."),
    task: str | None = typer.Option(None, "--task", help="Pin one task name."),
    rounds: int | None = typer.Option(None, "--rounds", help="Override configured round count."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan only; do not run Horizon."),
    resume: str | None = typer.Option(
        None, "--resume",
        help="Resume a run: pass a run id (e.g. 0007), or 'latest' for the most recent. Recovers an interrupted round; if the run already finished its rounds cleanly, runs another batch of forward rounds on the same focus. Combine with --rounds N to set that batch size.",
    ),
    backend: str = typer.Option(
        "default", "--backend",
        help="'default' streams a headless transcript (orchestrated). 'interactive' hands the terminal to the engine for a single role so you can type prompts (claude/codex sessions are still parsed into the Log) — use with `ground` or `horizon`.",
    ),
    run_id: str | None = typer.Option(
        None, "--run",
        help="Append a single-role session to this run id (created if new) instead of allocating a fresh run — so hand-driving ground/horizon into one run keeps the logs and dashboard grouped. Use with `ground` or `horizon`.",
    ),
    round_index: int | None = typer.Option(
        None, "--round",
        help="Round number for a single-role session (used with --run), so its metadata and commit trailer match the automatic alternation.",
    ),
    no_dashboard: bool = typer.Option(False, "--no-dashboard", help="Do not start the live dashboard alongside the run."),
    host: str = typer.Option(LOCAL_DASHBOARD_HOST, "--host", help="Dashboard host to bind during the run."),
    port: int = typer.Option(8765, "--port", help="Dashboard port to bind during the run."),
    public: bool = typer.Option(
        False,
        "--public",
        help="Bind the run dashboard to all IPv4 interfaces (equivalent to --host 0.0.0.0).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout."),
) -> None:
    """Run collaboration rounds.

    Targets resolve in order **task id > roadmap item id > project/file**: a task
    id runs that human-created task; a roadmap item id infers and runs a task for
    that mathematical milestone; a project name / file / `.` runs an ad-hoc task.
    `*` runs all queued tasks. A single role — `ground` (one planning session) or
    `horizon` (one prover session) — runs just that role. Add `--backend
    interactive` to drive a role in a live terminal.
    """
    try:
        dashboard_host = resolve_dashboard_host(host, public)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    RunCommand(
        ctx.obj["root"],
        targets=tuple(targets or ()),
        task=task,
        rounds=rounds,
        dry_run=dry_run,
        resume=resume,
        backend=backend,
        run_id=run_id,
        round_index=round_index,
        as_json=as_json,
        dashboard=not no_dashboard,
        dashboard_host=dashboard_host,
        dashboard_port=port,
    ).run()
