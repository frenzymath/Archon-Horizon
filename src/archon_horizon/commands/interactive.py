"""Launch a configured harness as an *interactive* TTY session.

Horizon's normal :class:`~archon_horizon.harnesses.base.Harness` seam streams a
machine-readable transcript (headless, ``-p``/``--json``) so the orchestrator can
parse it. But sometimes a human wants to *drive* an agent directly — type
follow-up prompts, steer it, ask questions. For that we build a raw interactive
argv from a role's harness config and hand the terminal straight to the engine
(no ``-p``, no transcript). Shared by ``horizon init``'s post-init advisor,
``horizon discuss``, and ``horizon run <role> --backend interactive``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class InteractiveLaunch:
    argv: list[str]
    env: dict[str, str]
    description: str
    # Which engine family this launch drives, so the transcript tailer knows how to
    # find and parse the engine's on-disk session file. "generic" = not parseable.
    engine: str = "generic"
    # For engines that accept a caller-chosen session id (claude), the id we pinned
    # so we can locate the exact session-store file. None for engines we can't pin.
    session_id: str | None = None


def _argv_with_prompt(argv: list[str], prompt: str) -> list[str]:
    from archon_horizon.harnesses.command import PROMPT_TOKEN

    if PROMPT_TOKEN in argv:
        return [prompt if token == PROMPT_TOKEN else token for token in argv]
    return [*argv, prompt]


def build_interactive_launch(
    harness, prompt: str, *, resume_session_id: str | None = None
) -> InteractiveLaunch | None:
    """Build an interactive launch from a resolved harness config.

    Returns ``None`` for the ``null`` harness (nothing to launch). Raises
    ``ValueError`` when the engine binary is missing or the kind has no
    interactive form. ``resume_session_id`` continues a prior engine conversation
    (claude ``--resume``) instead of starting a fresh one."""
    from archon_horizon.config.harnesses import _env_overrides, _resolve_claude_provider

    env = dict(os.environ)
    description = f"{harness.kind} harness {harness.name!r}"

    if harness.kind == "claude-code":
        from archon_horizon.config.harnesses import (
            _claude_effort_flag,
            _claude_thinking_budget,
            _claude_ultracode,
            _config_dir,
        )

        model, provider_env = _resolve_claude_provider(harness)
        # Build the override set the way headless does (harness env block + provider
        # env + pinned dirs/budget), then apply it OVER the ambient environment so a
        # per-harness setting wins over an ambient one — matching _build_claude_code,
        # where env_overrides update os.environ (overrides win).
        overrides = {**provider_env, **_env_overrides(harness)}
        # Pin the per-harness config/home (auth, user-level skills, session store)
        # exactly as headless — otherwise an interactive session runs under the
        # ambient/default ~/.claude and multi-account isolation breaks.
        config_dir = _config_dir(harness)
        if config_dir:
            overrides.setdefault("CLAUDE_CONFIG_DIR", config_dir)
        # A raw-integer effort is an explicit MAX_THINKING_TOKENS budget (a named tier
        # or ultracode goes on argv below instead).
        budget = _claude_thinking_budget(harness)
        if budget is not None:
            overrides.setdefault("MAX_THINKING_TOKENS", str(budget))
        env.update(overrides)
        argv = ["claude"]
        if model:
            argv += ["--model", model]
        # Reasoning effort must be carried into the interactive session too, exactly
        # as the headless build does — otherwise the TUI silently runs at claude's
        # default and the human has to re-set it with `/effort` every time. A named
        # tier → `--effort`; ultracode → `--settings '{"ultracode": true}'` (xhigh +
        # workflows); a raw-integer budget → MAX_THINKING_TOKENS (set above).
        effort_flag = _claude_effort_flag(harness)
        if effort_flag:
            argv += ["--effort", effort_flag]
        if _claude_ultracode(harness):
            argv += ["--settings", json.dumps({"ultracode": True})]
        # Honour the harness's permission-bypass setting (default True) like headless,
        # so a session configured to skip prompts isn't gated only when interactive.
        if bool(harness.options.get("skip_permissions", True)):
            argv.append("--dangerously-skip-permissions")
        if resume_session_id:
            # Continue the recorded conversation in place; --resume keeps the same
            # session id, so we still know (and can tail) its on-disk store file.
            session_id = resume_session_id
            argv += ["--resume", session_id]
            description += f" (resuming session {session_id})"
        else:
            # Pin a session id so we know the exact session-store file claude will
            # write (`<config>/projects/<sanitized-cwd>/<session-id>.jsonl`) and can
            # tail it live even though a foreground TUI streams nothing to our stdout.
            session_id = str(uuid.uuid4())
            argv += ["--session-id", session_id]
        argv += list(harness.args)
        if shutil.which(argv[0]) is None:
            raise ValueError("Claude Code is not installed; run `horizon setup` or install `claude`")
        return InteractiveLaunch(
            _argv_with_prompt(argv, prompt), env, description, engine="claude", session_id=session_id
        )

    if harness.kind == "codex":
        from archon_horizon.config.harnesses import _config_dir, _effort_label

        # Same precedence as headless: overrides win over the ambient environment.
        overrides = dict(_env_overrides(harness))
        # Pin CODEX_HOME (config/auth + rollout logs) to the per-harness home like
        # headless, so an interactive session uses the configured account, not ~/.codex.
        config_dir = _config_dir(harness)
        if config_dir:
            overrides.setdefault("CODEX_HOME", config_dir)
        env.update(overrides)
        argv = ["codex"]
        if harness.model:
            argv += ["-m", harness.model]
        # Normalise effort exactly as headless: `_effort_label` drops sentinels like
        # `default`/`none`/`auto` (which codex would reject and refuse to start on)
        # and lower-cases named tiers, instead of forwarding the raw option verbatim.
        effort = _effort_label(harness)
        if effort:
            argv += ["-c", f"model_reasoning_effort={effort}"]
        # Sandbox: an explicit `options.sandbox` wins; otherwise bypass (default True)
        # as headless does, so the interactive session isn't stuck behind approvals a
        # configured-to-bypass harness never sees.
        sandbox = str(harness.options.get("sandbox") or "").strip()
        if sandbox:
            argv += ["--sandbox", sandbox]
        elif bool(harness.options.get("bypass_sandbox", True)):
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        argv += list(harness.args)
        if shutil.which(argv[0]) is None:
            raise ValueError("Codex is not installed; install `codex` or choose another harness")
        # Codex names its rollout file itself (we can't pin an id), so the tailer
        # detects the new session file that appears under $CODEX_HOME/sessions.
        return InteractiveLaunch(_argv_with_prompt(argv, prompt), env, description, engine="codex")

    if harness.kind in {"command", "external-agent"}:
        if not harness.command:
            raise ValueError(f"Harness {harness.name!r} needs a command")
        env.update(_env_overrides(harness))
        return InteractiveLaunch(_argv_with_prompt([harness.command, *harness.args], prompt), env, description)

    if harness.kind == "null":
        return None

    raise ValueError(f"no interactive launcher for harness kind {harness.kind!r}")


def _harness_for_role(cfg, role: str):
    """Resolve the harness config backing ``ground`` or ``horizon``."""
    name = cfg.ground_harness if role == "ground" else cfg.horizon_harness
    if not name:
        raise ValueError(f"config.yaml does not define a {role.capitalize()} harness")
    try:
        return cfg.harnesses[name]
    except KeyError as exc:
        raise ValueError(f"{role.capitalize()} harness {name!r} is not defined") from exc


def interactive_launch_for_role(
    root: Path, role: str, prompt: str, *, resume_session_id: str | None = None
) -> InteractiveLaunch | None:
    """Build an interactive launch from the harness backing ``role``."""
    from archon_horizon.config.loader import load_config

    cfg = load_config(root)
    return build_interactive_launch(
        _harness_for_role(cfg, role), prompt, resume_session_id=resume_session_id
    )


def run_interactive(launch: InteractiveLaunch, cwd: Path) -> int:
    """Hand the terminal to the engine; returns its exit code."""
    return subprocess.run(launch.argv, cwd=cwd, env=launch.env, check=False).returncode


# ── interactive-with-parsing ────────────────────────────────────────────────
#
# A foreground TUI streams nothing to our stdout, so — unlike the headless
# Harness — we can't parse the conversation from a pipe. Instead we tail the
# engine's OWN on-disk session file as it grows and convert each line into
# Horizon's canonical transcript events (reusing the same per-engine parsers the
# headless path uses), so an interactive run still shows up in the Log/dashboard
# and stays `--resume`-able via the recorded session id.

_TAIL_POLL_LOCATE_S = 0.3
_TAIL_POLL_DRAIN_S = 0.4


def _claude_projects_dir(env: dict[str, str]) -> Path:
    base = env.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return Path(base) / "projects"


def _codex_sessions_dir(env: dict[str, str]) -> Path:
    home = env.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return Path(home) / "sessions"


class _InteractiveTailer(threading.Thread):
    """Background thread that mirrors an engine's on-disk session file into a
    Horizon transcript sink while the human drives the foreground TUI.

    Byte-offset + leftover buffering makes it partial-line safe: a half-written
    JSON line is held back until its newline arrives, so we never parse a torn
    line. Locating is engine-specific; parsing reuses the shared per-engine
    parsers, so live-tail and headless ingestion stay identical."""

    def __init__(self, launch: InteractiveLaunch, sink) -> None:
        super().__init__(name="horizon-interactive-tailer", daemon=True)
        self._launch = launch
        self._sink = sink
        self._stop = threading.Event()
        self._file: Path | None = None
        self._offset = 0
        self._leftover = b""
        self._parser = _parser_for(launch.engine)
        # For codex we can't pin the session id, so snapshot the rollout files that
        # exist BEFORE launch and treat the first new one as this session's file.
        self._codex_seen: set[str] = set()
        if launch.engine == "codex":
            try:
                self._codex_seen = {p.as_posix() for p in _codex_sessions_dir(launch.env).glob("**/rollout-*.jsonl")}
            except OSError:
                self._codex_seen = set()

    def _locate(self) -> Path | None:
        try:
            if self._launch.engine == "claude" and self._launch.session_id:
                matches = sorted(_claude_projects_dir(self._launch.env).glob(f"**/{self._launch.session_id}.jsonl"))
                return matches[0] if matches else None
            if self._launch.engine == "codex":
                fresh = [
                    p for p in _codex_sessions_dir(self._launch.env).glob("**/rollout-*.jsonl")
                    if p.as_posix() not in self._codex_seen
                ]
                # Newest new rollout is this session's (a spawned subagent gets its
                # own file, but the main thread's is the earliest of the new set).
                return min(fresh, key=lambda p: p.stat().st_mtime) if fresh else None
        except OSError:
            return None
        return None

    def _drain(self) -> None:
        if self._file is None or self._parser is None:
            return
        try:
            with self._file.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
                self._offset = handle.tell()
        except OSError:
            return
        if not chunk:
            return
        buffer = self._leftover + chunk
        lines = buffer.split(b"\n")
        self._leftover = lines.pop()  # trailing (possibly incomplete) fragment
        for raw in lines:
            if not raw.strip():
                continue
            try:
                for event in self._parser(raw.decode("utf-8", "replace")):
                    self._sink.emit(event)
            except Exception:
                continue  # a single malformed line must never kill the tailer

    def run(self) -> None:
        if self._parser is None:
            return  # generic engine: nothing to parse
        while not self._stop.wait(_TAIL_POLL_LOCATE_S):
            self._file = self._locate()
            if self._file is not None:
                break
        while not self._stop.wait(_TAIL_POLL_DRAIN_S):
            self._drain()

    def stop(self) -> None:
        # One last locate+drain so the tail written just before the human quit is
        # captured (the drain loop may have exited between the final write and now).
        if self._file is None:
            self._file = self._locate()
        self._stop.set()
        self.join(timeout=2.0)
        self._drain()


def _parser_for(engine: str):
    from archon_horizon.transcript.parsers import parse_claude_line, parse_codex_rollout_line

    return {"claude": parse_claude_line, "codex": parse_codex_rollout_line}.get(engine)


def run_interactive_captured(
    launch: InteractiveLaunch, cwd: Path, *, transcript_path: Path, role: str, seed_prompt: str = ""
) -> int:
    """Drive the engine interactively (foreground TTY) while tailing its on-disk
    session into ``transcript_path`` as canonical Horizon events. Returns the
    engine's exit code. Falls back to a raw launch (no capture) for a ``generic``
    engine that has no parseable session file."""
    from archon_horizon.transcript.model import TranscriptEvent, TranscriptKind
    from archon_horizon.transcript.sink import JsonlTranscriptSink

    sink = JsonlTranscriptSink(transcript_path)
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_START, data={"role": role, "interactive": True}))
    if launch.session_id:
        sink.emit(TranscriptEvent(
            TranscriptKind.SESSION_META, data={"session_id": launch.session_id, "interactive": True}
        ))
    if seed_prompt:
        sink.emit(TranscriptEvent(TranscriptKind.TEXT, text=seed_prompt, data={"role": "prompt"}))

    tailer = _InteractiveTailer(launch, sink)
    tailer.start()
    try:
        returncode = subprocess.run(launch.argv, cwd=cwd, env=launch.env, check=False).returncode
    finally:
        tailer.stop()
    sink.emit(TranscriptEvent(
        TranscriptKind.SESSION_END, data={"ok": returncode == 0, "interactive": True}
    ))
    return returncode


# ── seed prompts ─────────────────────────────────────────────────────────────
#
# Interactive sessions are *human-driven*, so we don't compose the full
# orchestrated agent prompt (which assumes a specific task/round). Instead we seed
# a short role brief that points the engine at the workspace, the docs, and the
# on-disk state, then let the human steer from there.

_ORIENTATION = """\
You are running inside an **Archon Horizon** workspace at `{root}`.

Archon Horizon orchestrates AI agents that formalize mathematics in Lean 4 across
multiple projects. To understand the system, read (in the *package install*, not
necessarily this workspace):

- `README.md` — the detailed, self-contained reference for the whole tool.
- `docs/` — deeper guides per topic (architecture, workspaces, orchestration,
  inboxes, blueprints/leandag, dashboard/search, CLI reference).

This workspace's live state lives under `.archon-horizon/` — `runs/` (session
transcripts and reports), `tasks/`, `inbox/`, `blueprints/`, `roadmap`,
`memory.md` — and its manifest is `config.yaml`. Read those to see the current
status and what recent runs did. Prefer the `horizon` CLI for changes."""

_ROLE_BRIEF = {
    "ground": """\
Act as the **Ground agent**: the human-aligned strategist. You maintain the
blueprints, dependency DAGs, roadmap, tasks, memory, and inboxes — and supervise
the Long Horizon prover. You do not run long Lean proof searches yourself.""",
    "horizon": """\
Act as the **Long Horizon agent**: the autonomous prover. You turn blueprint
nodes into checked Lean — read the blueprint node first, build with `lake`,
diagnose compiler errors, and repair proofs. Report what you proved and what
remains.""",
}

_DISCUSS_BRIEF = """\
You are the **discuss** agent: a human-facing companion, essentially the Ground
agent but here purely to talk with the human and do what they ask. Your job:

- Explain the current status of this workspace and what recent runs did (read the
  run transcripts/reports under `.archon-horizon/runs/`).
- Answer questions about Archon Horizon itself — read `README.md`/`docs/` and,
  when a detail isn't documented, the package source.
- Help manage the workspace: add or adjust projects, tasks, inbox items, roadmap
  entries, blueprints — using the `horizon` CLI.

Rules of engagement:
- Only *modify* anything when the human explicitly asks you to. Otherwise
  explain, propose, and wait.
- Be concrete: cite exact files, task ids, and commands.
- Start by briefly greeting the human and offering a short status summary, then
  ask what they'd like to do."""


def interactive_role_prompt(
    root: Path, role: str, focus: tuple[str, ...] = (), *, resuming: bool = False
) -> str:
    """Seed prompt for an interactive `horizon run` session.

    ``focus`` is the task ids / projects / files the human targeted (e.g.
    ``horizon run T16``); when present it is appended so the session starts on that
    work instead of the generic role brief. ``resuming`` frames it as continuing an
    earlier session (the engine conversation may already be in context)."""
    brief = _ROLE_BRIEF.get(role, _ROLE_BRIEF["ground"])
    items = ", ".join(f"`{f}`" for f in focus)
    focus_hint = ""
    if resuming:
        target = f" on {items}" if items else ""
        focus_hint = (
            f"\n\nYou are RESUMING an earlier interactive session{target}. If that "
            "conversation is already in context, pick up where you left off — re-read "
            "the file(s) you were editing and any build output to refresh. Otherwise, "
            "orient from the workspace state above. Either way, briefly say where "
            "things stand and what you propose next, then wait for the human (they are "
            "driving)."
        )
    elif focus:
        focus_hint = (
            f"\n\nThe human launched this session focused on: {items}. Start there — "
            "read its blueprint node(s)/task details and the relevant Lean, orient "
            "yourself, then briefly say what you see and propose the first step before "
            "diving in (they are driving, so check in rather than running autonomously)."
        )
    return f"{_ORIENTATION.format(root=root)}\n\n{brief}{focus_hint}\n"


def discuss_prompt(root: Path) -> str:
    """Seed prompt for the `horizon discuss` companion agent."""
    return f"{_ORIENTATION.format(root=root)}\n\n{_DISCUSS_BRIEF}\n"
