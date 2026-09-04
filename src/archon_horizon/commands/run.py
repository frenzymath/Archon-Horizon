"""Typer-decorated ``run`` entry point."""

from __future__ import annotations

import hashlib
import dataclasses
import os
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
from archon_horizon.transcript.sink import latest_report_text

from .dashboard import LOCAL_DASHBOARD_HOST, resolve_dashboard_host
from .ps import clear_process_marker, write_process_marker
from .shared import emit_json, inbox_providers, load_workspace

# The single-agent run target: ``horizon run horizon`` drives exactly one
# Horizon session over the current focus.
ROLE_TARGETS = ("horizon",)


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
        bare: bool = False,
        supervisor: bool = False,
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
        self.bare = bare
        self.supervisor = supervisor
        self.run_id = (run_id or "").strip() or None
        self.round_index = round_index
        self.as_json = as_json
        self.dashboard = dashboard
        self.dashboard_host = dashboard_host
        self.dashboard_port = dashboard_port

    def run(self) -> None:
        if not self.targets and self.task:
            self.targets = (self.task,)
        # `--bare` is a lightweight *interactive* seed, so it implies that backend.
        if self.bare:
            self.backend = "interactive"

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
            self._refresh_mcp_config(cfg, workspace)
            _, providers = inbox_providers(cfg, workspace)
            orch = build_orchestrator(self.root, registry=HarnessRegistry(), inbox_providers=providers)

            if self.resume is not None:
                run = self._resume_run(orch)
                reports = orch.run(run, resume=True, rounds_override=self.rounds)
                self._emit_reports(reports)
                self._exit_if_paused(orch)
                return

            if not self.targets and not self.supervisor:
                log.error("Specify what to run: `horizon run .`, `horizon run '*'`, `horizon`, task names, project names, or files (or `--supervisor` for all queued work).")
                raise typer.Exit(1)

            reports = orch.run(self._build_run(orch, cfg.rounds), dry_run=self.dry_run)
            self._emit_reports(reports)
            self._exit_if_paused(orch)

    @staticmethod
    def _exit_if_paused(orch) -> None:
        """Distinct exit code (3) when the run paused on a limit/budget, so an
        external relaunch loop can tell 'paused, resumable' from success/failure.
        The pause details live in ``runs/<id>/paused.json``."""
        paused = getattr(orch, "last_paused", None)
        if not paused:
            return
        hint = paused.get("retry_after_s")
        wait = f" (engine advertised retry after {hint}s)" if hint else ""
        log.warn(
            f"Run paused: {paused.get('reason')}{wait}. State is on disk — resume with "
            f"`{paused.get('resume')}`."
        )
        raise typer.Exit(3)

    def _build_run(self, orch, default_rounds: int) -> RunRecord:
        """Every launch shape reduces to a focus plus a RunRecord:

        - ``horizon run horizon [targets…]`` drives ONE session over the focus
          (with ``--run``/``--round`` to append it into an existing run);
        - ``--supervisor`` drives N rounds over the focus (empty focus = all
          queued work);
        - plain targets drive N rounds pinned to the resolved focus.
        """
        role_session = bool(self.targets) and self.targets[0] in ROLE_TARGETS
        focus_targets = tuple(self.targets[1:] if role_session else self.targets)
        focus = self._resolve_focus(orch, focus_targets) if focus_targets else Focus()
        rounds = self.rounds or (1 if role_session else default_rounds)
        return RunRecord(
            id=self.run_id or "",
            focus=focus,
            rounds_requested=rounds,
            start_round=self.round_index or 0,
        )

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

    def _config_interactive_role(self) -> str | None:
        """The role to launch interactively when its harness declares
        ``backend: interactive``, else ``None``.

        Interactive is a single human-driven session. Every target shape drives a
        Horizon session (a task, a project, ``.``/``*``, or ``horizon run
        horizon``), so the Horizon harness's opt-in governs. A ``--resume`` counts
        too: it continues the interrupted run interactively (resuming the engine
        conversation)."""
        try:
            cfg, _ = load_workspace(self.root)
        except Exception:
            return None

        def declares_interactive(name: str | None) -> bool:
            harness = cfg.harnesses.get(name) if name else None
            return harness is not None and str(
                harness.options.get("backend") or ""
            ).strip().lower() == "interactive"

        # Every target shape runs a Horizon session, so the Horizon harness's opt-in
        # governs — seeded with whatever focus was requested.
        return "horizon" if declares_interactive(cfg.horizon_harness) else None

    def _interactive_resume_run_id(self, run_logs) -> str | None:
        """Normalize the interactive ``--resume`` selector to an existing run id."""
        run_id = (self.resume or "").strip()
        if run_id.lower() in ("", "latest", "last"):
            ids = run_logs.ids()
            run_id = ids[-1] if ids else ""
        elif run_id.isdigit():
            run_id = f"{int(run_id):04d}"
        if not run_id or run_id not in run_logs.ids():
            return None
        return run_id

    def _recover_interactive_resume(
        self, role: str
    ) -> tuple[str | None, tuple[str, ...], str | None]:
        """For an interactive ``--resume``, find the run's last ``role`` session and
        return ``(engine_session_id, focus, project)`` — the id to hand
        ``claude --resume``, the task to re-seed, and the original project cwd.
        Values may be empty when the run recorded none of them."""
        from archon_horizon.config.loader import build_stores

        try:
            _, workspace = load_workspace(self.root)
            stores = build_stores(workspace)
            run_logs = stores.run_logs
            run_id = self._interactive_resume_run_id(run_logs)
            if not run_id:
                return None, (), None
            sessions = run_logs.get(run_id).sessions()
        except Exception:
            return None, (), None
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
            project = str(meta.get("project") or "") or None
            if project is None:
                names = meta.get("projects")
                if isinstance(names, list) and names and isinstance(names[0], str):
                    project = names[0]
            if isinstance(sid, str) and sid:
                return sid, focus, project
            if focus:
                # No engine id (older/interrupted-before-first-turn), but we at least
                # recovered the task — a fresh session seeded with it is still useful.
                return None, focus, project
        try:
            run = stores.runs.get(run_id)
            task_id = run.focus.task or (run.focus.tasks[-1] if run.focus.tasks else None)
            return None, (task_id,) if task_id else (), None
        except Exception:
            return None, (), None

    def _run_interactive(self) -> None:
        """Launch the role's harness as an interactive TTY session, seeded with a
        role prompt the human can then steer.

        Unlike a raw TTY launch, this tails the engine's own on-disk session file
        into a Horizon run session as it grows, so a human-driven interactive
        session still shows up in the Log/dashboard (parsed like a headless run) and
        stays ``--resume``-able via the recorded engine session id."""
        from archon_horizon.config.loader import build_stores
        from archon_horizon.core.clock import utc_now
        from archon_horizon.core.events import Event
        from archon_horizon.core.scratch import remove_session_tmp, scratch_environment
        from archon_horizon.harnesses.command import _horizon_bin
        from archon_horizon.vcs.git import WorkspaceGit, install_ledger_git_wrapper
        from archon_horizon.vcs.integration import (
            integrate_workspace_baseline,
            integrate_workspace_session,
        )

        from .interactive import (
            claude_session_file,
            horizon_seed_prompt,
            interactive_launch_for_role,
            run_interactive,
            run_interactive_captured,
        )

        # `focus` = the non-role targets (task ids / projects / files) the human
        # asked for; the interactive session is seeded to start there.
        focus = tuple(t for t in self.targets if t not in ROLE_TARGETS)
        cfg, workspace = load_workspace(self.root)
        self._install_native_subagents(cfg, workspace)
        self._refresh_mcp_config(cfg, workspace)
        stores = build_stores(workspace)
        # Prefer the role picked by config routing (`_config_interactive_role`); the
        # plain `--backend interactive` CLI path falls back to the target shape.
        # Only one role exists now (horizon); interactive always drives it.
        role = getattr(self, "_interactive_role", None) or "horizon"

        # `--resume` interactively continues the interrupted run's engine
        # conversation: recover its last matching session's engine id (for a true
        # `claude --resume`), the task it was on, and the project cwd Claude used.
        resume_session_id: str | None = None
        resume_project: str | None = None
        resumed_run_id: str | None = None
        if self.resume is not None:
            resumed_run_id = self._interactive_resume_run_id(stores.run_logs)
            if resumed_run_id is None:
                log.error(f"Cannot resume run {self.resume!r}: no run record found.")
                raise typer.Exit(1)
            resume_session_id, resume_focus, resume_project = (
                self._recover_interactive_resume(role)
            )
            if resume_focus and not focus:
                focus = resume_focus
            if resume_session_id is None:
                log.info("No resumable engine session found for that run; starting a "
                         "fresh interactive session seeded with its focus instead.")

        task = None
        for target in ((self.task,) if self.task else focus):
            if not target:
                continue
            try:
                task = stores.tasks.get(target)
                break
            except Exception:
                continue
        task_id = task.id if task is not None else self.task
        if task_id is None and self.resume is not None and focus:
            task_id = focus[0]
        if task is not None:
            projects = tuple(task.projects) or ((task.project,) if task.project else ())
        else:
            projects = tuple(name for name in focus if name in workspace.projects)

        # Claude stores conversations under a cwd-scoped project directory.  Use
        # the resumed session's recorded project (or the task's primary project),
        # otherwise a valid id created by a headless project run is invisible to an
        # interactive process launched from the workspace root.
        cwd_project = resume_project or (task.project if task is not None else None)
        interactive_cwd = self.root
        if cwd_project and cwd_project in workspace.projects:
            interactive_cwd = workspace.project_path(cwd_project)

        # Every interactive session gets the lightweight seed: the only
        # instruction is to load the `horizon` skill, then wait for the user —
        # no composed role brief. (`--bare` is now the default and only shape.)
        # The UI still records the session identically (captured path below).
        prompt = horizon_seed_prompt(
            self.root.resolve(), focus=focus, resuming=self.resume is not None
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

        attempted_resume_session_id = resume_session_id
        resume_fallback_reason: str | None = None
        resume_file = claude_session_file(launch) if resume_session_id else None
        if resume_session_id and launch.engine == "claude" and resume_file is None:
            resume_fallback_reason = "conversation-not-found"
            log.warn(
                f"Claude conversation {resume_session_id} is not present in this "
                "harness's session store; starting a fresh interactive session in "
                f"run {resumed_run_id} with the recovered run context."
            )
            try:
                launch = interactive_launch_for_role(self.root, role, prompt)
            except Exception as exc:
                log.error(f"Could not launch a fresh interactive {role} session: {exc}")
                raise typer.Exit(1)
            if launch is None:
                log.error(f"The {role} harness is 'null'; nothing to launch interactively.")
                raise typer.Exit(1)
        log.info(f"Launching an interactive {role} session using {launch.description}.")

        # A generic engine has no parseable session file, so just hand over the TTY.
        if launch.engine == "generic":
            scratch_dir, scratch_env = scratch_environment(
                workspace,
                run_id=self.run_id or "interactive",
                session="interactive",
                role=role,
            )
            launch = dataclasses.replace(launch, env={**launch.env, **scratch_env})
            try:
                run_interactive(launch, interactive_cwd)
            finally:
                remove_session_tmp(scratch_dir)
            return

        if resumed_run_id is not None:
            runlog = stores.run_logs.get(resumed_run_id)
        else:
            runlog = stores.run_logs.allocate()
            run = RunRecord(
                id=runlog.id,
                focus=Focus(
                    projects=projects,
                    task=task_id,
                    tasks=(task_id,) if task_id else (),
                ),
                rounds_requested=1,
            )
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
            "task_id": task_id,
            "project": cwd_project,
            "projects": list(projects),
            "started_at": utc_now().isoformat(),
        }
        if attempted_resume_session_id:
            base_meta["resumed_engine_session_id"] = attempted_resume_session_id
        if resume_fallback_reason:
            base_meta["resume_fallback_reason"] = resume_fallback_reason
        session.write_meta({**base_meta, "status": "running"})
        log.info(f"Recording this interactive session under run {runlog.id} — visible in the dashboard/Log.")

        baseline = integrate_workspace_baseline(
            workspace,
            run_id=runlog.id,
            projects=projects,
        )
        ledger = WorkspaceGit(workspace.root)
        session_base_sha = baseline.sha or ledger.current_sha()
        base_meta["workspace_base_sha"] = session_base_sha
        session.write_meta({**base_meta, "status": "running"})
        stores.events.append(Event(
            type="workspace.run_baseline",
            id=hashlib.sha256(f"baseline:{runlog.id}".encode()).hexdigest(),
            actor="orchestrator",
            data={
                "run_id": runlog.id,
                "sha": baseline.sha,
                "changed": baseline.changed,
                "files": list(baseline.files),
                "projects": list(projects),
            },
        ))

        scratch_dir, scratch_env = scratch_environment(
            workspace,
            run_id=runlog.id,
            session=session.name,
            role=role,
        )
        session_env = {
            **launch.env,
            **scratch_env,
            "ARCHON_HORIZON_ROOT": str(workspace.root.resolve()),
            "ARCHON_HORIZON_SKILL": str(
                (workspace.root / ".claude" / "skills" / "horizon" / "SKILL.md").resolve()
            ),
            "ARCHON_HORIZON_RUN": runlog.id,
            "ARCHON_HORIZON_SESSION": session.name,
            "ARCHON_HORIZON_SESSION_DIR": str(session.path.resolve()),
            "ARCHON_HORIZON_AGENT_ROLE": role,
            "ARCHON_HORIZON_ROUND": "0",
            "ARCHON_HORIZON_ROUNDS": "1",
            "HORIZON_LEDGER_GIT_DIR": str(WorkspaceGit(workspace.root).git_dir),
            "HORIZON_LEDGER_WORK_TREE": str(workspace.root.resolve()),
        }
        if task_id:
            session_env["ARCHON_HORIZON_TASK"] = task_id
        if task is not None and task.title:
            session_env["ARCHON_HORIZON_TASK_TITLE"] = task.title
        if projects:
            session_env["ARCHON_HORIZON_PROJECTS"] = ",".join(projects)
        wrapper = install_ledger_git_wrapper(workspace.state_path)
        if wrapper is not None:
            session_env["HORIZON_GIT"] = str(wrapper.resolve())
        horizon_bin = _horizon_bin()
        if horizon_bin:
            session_env.setdefault("HORIZON_BIN", horizon_bin)
            session_env["PATH"] = (
                str(Path(horizon_bin).parent) + os.pathsep + session_env.get("PATH", "")
            )
        launch = dataclasses.replace(launch, env=session_env)

        resume_fingerprint: tuple[int, int] | None = None
        if resume_file is not None and resume_fallback_reason is None:
            try:
                stat = resume_file.stat()
                resume_fingerprint = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                resume_fingerprint = None
        write_process_marker(
            runlog.path,
            task=task_id,
            task_title=task.title if task is not None else "",
            session=session.name,
            projects=list(projects),
            interactive=True,
        )
        try:
            returncode = run_interactive_captured(
                launch,
                interactive_cwd,
                transcript_path=session.transcript_path,
                role=role,
                seed_prompt=prompt,
            )
            if (
                returncode != 0
                and attempted_resume_session_id
                and resume_fallback_reason is None
                and resume_file is not None
                and resume_fingerprint is not None
            ):
                try:
                    stat = resume_file.stat()
                    resume_unchanged = resume_fingerprint == (stat.st_size, stat.st_mtime_ns)
                except OSError:
                    resume_unchanged = True
                if resume_unchanged:
                    resume_fallback_reason = "native-resume-rejected"
                    log.warn(
                        f"Claude could not resume conversation {attempted_resume_session_id}; "
                        f"starting a fresh interactive session in run {runlog.id} with "
                        "the recovered run context."
                    )
                    try:
                        fresh_launch = interactive_launch_for_role(self.root, role, prompt)
                    except Exception as exc:
                        log.error(f"Could not launch a fresh interactive {role} session: {exc}")
                        fresh_launch = None
                    if fresh_launch is not None:
                        launch = dataclasses.replace(fresh_launch, env=session_env)
                        base_meta.update({
                            "engine": launch.engine,
                            "engine_session_id": launch.session_id,
                            "resume_fallback_reason": resume_fallback_reason,
                        })
                        session.write_meta({**base_meta, "status": "running"})
                        returncode = run_interactive_captured(
                            launch,
                            interactive_cwd,
                            transcript_path=session.transcript_path,
                            role=role,
                            seed_prompt=prompt,
                        )
        finally:
            clear_process_marker(runlog.path)
            remove_session_tmp(scratch_dir)

        agent_head = ledger.current_sha()
        candidate_shas = ledger.commit_shas_between(session_base_sha, agent_head)
        candidate_rows = ledger.commits_detailed_by_refs(candidate_shas)
        commit_shas = [
            row["sha"] for row in candidate_rows
            if not row.get("session") or row.get("session") == session.name
        ]
        report_text = latest_report_text(session.transcript_path)
        if not report_text:
            report_text = (
                f"# Interactive {role} session\n\n"
                f"A human-driven interactive session (engine: `{launch.engine}`). Its conversation was "
                f"mirrored into this run's transcript as it happened. Exit code {returncode}.\n"
            )
        (session.path / "report.md").write_text(report_text.rstrip() + "\n", "utf-8")
        integration = integrate_workspace_session(
            workspace,
            run_id=runlog.id,
            session=session.name,
            role=role,
            round_index=0,
            task_id=task_id,
            projects=projects,
        )
        stores.events.append(Event(
            type="workspace.session.integrated",
            id=hashlib.sha256(f"integration:{runlog.id}:{session.name}".encode()).hexdigest(),
            actor="orchestrator",
            data={
                "run_id": runlog.id,
                "session": session.name,
                "role": role,
                "task_id": task_id,
                "projects": list(integration.projects),
                "workspace_commit": integration.workspace_commit,
                "workspace_commit_error": integration.workspace_commit_error,
                "files": list(integration.workspace_files),
            },
        ))
        session.write_meta({
            **base_meta,
            "status": "ok" if returncode == 0 else "failed",
            "ended_at": utc_now().isoformat(),
            "returncode": returncode,
            "workspace_agent_sha": agent_head,
            "commit_shas": commit_shas,
            "workspace_commit": integration.workspace_commit,
        })
        if integration.workspace_commit_error:
            log.warn(f"Could not integrate interactive session: {integration.workspace_commit_error}")

    def _install_native_subagents(self, cfg, workspace) -> None:
        """Compile descriptors into each engine's workspace-local native agents.

        Done at run start so descriptor changes take effect on the next run,
        with no separate install step.
        """
        try:
            from archon_horizon.subagents.compile import install_subagents

            install_subagents(workspace.root, workspace.state_path / "subagents", cfg.harnesses)
        except Exception as exc:  # never block a run on optional subagent compile
            log.warn(f"Skipped subagent compilation: {exc}")

    def _refresh_mcp_config(self, cfg, workspace) -> None:
        """Refresh managed Lean MCP entries before an engine session starts.

        Workspaces can outlive the package version that initialized them. Doing
        this at run start keeps Codex's project-local config and Claude's
        ``.mcp.json`` aligned without requiring a separate upgrade command.
        Hand-added servers remain untouched by the merge helpers.
        """
        try:
            from archon_horizon.config.mcp import install_mcp_for_harnesses, write_mcp_config

            write_mcp_config(workspace.root / ".mcp.json")
            install_mcp_for_harnesses(cfg.harnesses, workspace.root)
        except Exception as exc:  # optional tooling must not block a run
            log.warn(f"Skipped MCP setup refresh: {exc}")

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
                    "tasks_unrunnable": list(report.tasks_unrunnable),
                })
                detail = (
                    f"ran {', '.join(report.tasks_run) or '-'}; "
                    f"blocked {', '.join(report.tasks_blocked) or '-'}; "
                    f"not runnable {', '.join(report.tasks_unrunnable) or '-'}"
                )
                if report.tasks_blocked or report.tasks_unrunnable:
                    status = "blocked"
                elif report.tasks_run:
                    status = "done"
                else:
                    status = "idle"
                rows.append((f"round {report.round_index}", status, detail))
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
    targets: list[str] = typer.Argument(None, help="Run target: '.', '*', 'horizon', a task id, a roadmap item id, project names, or files (resolved task > roadmap > project)."),
    task: str | None = typer.Option(None, "--task", help="Pin one task name."),
    rounds: int | None = typer.Option(None, "--rounds", help="Override configured round count."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan only; do not run Horizon."),
    resume: str | None = typer.Option(
        None, "--resume",
        help="Resume a run: pass a run id (e.g. 0007), or 'latest' for the most recent. Recovers an interrupted round; if the run already finished its rounds cleanly, runs another batch of forward rounds on the same focus. Combine with --rounds N to set that batch size.",
    ),
    backend: str = typer.Option(
        "default", "--backend",
        help="'default' streams a headless transcript (orchestrated). 'interactive' hands the terminal to the engine so you can type prompts (claude/codex sessions are still parsed into the Log).",
    ),
    bare: bool = typer.Option(
        False, "--bare",
        help="Shorthand for `--backend interactive`: an interactive session seeded only with 'load the `horizon` skill, then wait for you'. (All interactive sessions use this seed now.) The session is still recorded in the Log/dashboard.",
    ),
    supervisor: bool = typer.Option(
        False, "--supervisor",
        help="Lightweight automated loop: run `--rounds` Horizon-only sessions (no Ground role — the Horizon agent spawns a cleanup subagent itself when it wants). Stops cleanly on a usage-limit (state is on disk; just re-run to resume).",
    ),
    run_id: str | None = typer.Option(
        None, "--run",
        help="Append a single-role session to this run id (created if new) instead of allocating a fresh run — so hand-driving sessions into one run keeps the logs and dashboard grouped. Use with `horizon`.",
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
    `*` runs all queued tasks. `horizon` runs one prover session over the focus.
    Add `--backend interactive` to drive the session in a live terminal.
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
        bare=bare,
        supervisor=supervisor,
        run_id=run_id,
        round_index=round_index,
        as_json=as_json,
        dashboard=not no_dashboard,
        dashboard_host=dashboard_host,
        dashboard_port=port,
    ).run()
