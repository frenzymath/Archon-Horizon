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

import dataclasses
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from archon_horizon.transcript.model import TranscriptEvent, TranscriptKind


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
    """Resolve the harness config for an interactive session (horizon-only)."""
    name = cfg.horizon_harness
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
        self._codex_parent_id: str | None = None
        self._codex_children: dict[str, dict[str, object]] = {}
        self._codex_child_scan_at = 0.0
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
        if chunk:
            buffer = self._leftover + chunk
            lines = buffer.split(b"\n")
            self._leftover = lines.pop()  # trailing (possibly incomplete) fragment
            for raw in lines:
                if not raw.strip():
                    continue
                decoded = raw.decode("utf-8", "replace")
                try:
                    if self._launch.engine == "codex" and self._codex_parent_id is None:
                        obj = json.loads(decoded)
                        payload = obj.get("payload", {}) if isinstance(obj, dict) else {}
                        if obj.get("type") == "session_meta" and isinstance(payload, dict):
                            native_id = payload.get("id")
                            if isinstance(native_id, str) and native_id:
                                self._codex_parent_id = native_id
                    for event in self._parser(decoded):
                        self._sink.emit(event)
                except Exception:
                    continue  # a single malformed line must never kill the tailer
        if self._launch.engine == "codex":
            self._drain_codex_child_lifecycles()

    def _drain_codex_child_lifecycles(self) -> None:
        """Surface direct Codex child ``task_complete`` events in the parent log.

        Interactive Codex writes every spawned thread to its own rollout.  We do
        not mirror those large transcripts here; we only follow their metadata
        and terminal marker so the dashboard gets a compact dispatch/closure
        timeline while the parent TUI remains open.
        """
        if not self._codex_parent_id:
            return
        # Discover at a lower frequency than the parent-file drain: recursive
        # session discovery is cheap occasionally, but wasteful four times/sec.
        now = time.monotonic()
        if now >= self._codex_child_scan_at:
            self._codex_child_scan_at = now + 2.0
            try:
                for path in _codex_sessions_dir(self._launch.env).glob("**/rollout-*.jsonl"):
                    key = path.as_posix()
                    if path != self._file and key not in self._codex_seen:
                        self._codex_children.setdefault(key, {
                            "offset": 0, "leftover": b"", "accepted": None, "last_complete": None,
                        })
            except OSError:
                pass
        candidates = [Path(key) for key, state in self._codex_children.items() if state.get("accepted") is not False]
        for path in candidates:
            key = path.as_posix()
            state = self._codex_children[key]
            try:
                with path.open("rb") as handle:
                    handle.seek(int(state["offset"]))
                    chunk = handle.read()
                    state["offset"] = handle.tell()
            except (OSError, ValueError):
                continue
            if not chunk:
                continue
            buffer = state.get("leftover", b"") + chunk
            if not isinstance(buffer, bytes):
                buffer = chunk
            lines = buffer.split(b"\n")
            state["leftover"] = lines.pop()
            for raw in lines:
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8", "replace"))
                except (ValueError, TypeError):
                    continue
                payload = obj.get("payload", {}) if isinstance(obj, dict) else {}
                if not isinstance(payload, dict):
                    continue
                if obj.get("type") == "session_meta":
                    source = payload.get("source", {})
                    subagent = source.get("subagent", {}) if isinstance(source, dict) else {}
                    spawn = subagent.get("thread_spawn", {}) if isinstance(subagent, dict) else {}
                    if not isinstance(spawn, dict):
                        state["accepted"] = False
                        continue
                    state["accepted"] = spawn.get("parent_thread_id") == self._codex_parent_id
                    if state["accepted"]:
                        agent_path = spawn.get("agent_path")
                        if isinstance(agent_path, str) and agent_path:
                            state["agent_path"] = agent_path
                            state["name"] = agent_path.rstrip("/").rsplit("/", 1)[-1]
                        nickname = spawn.get("agent_nickname")
                        if isinstance(nickname, str) and nickname:
                            state["nickname"] = nickname
                        state["depth"] = spawn.get("depth")
                        state["started_at"] = obj.get("timestamp")
                    continue
                if state.get("accepted") is not True:
                    continue
                if obj.get("type") == "turn_context":
                    model = payload.get("model")
                    effort = payload.get("effort")
                    if isinstance(model, str) and model:
                        state["model"] = model
                    if isinstance(effort, str) and effort:
                        state["effort"] = effort
                if obj.get("type") == "event_msg" and payload.get("type") == "task_complete":
                    completed_at = obj.get("timestamp")
                    completed_epoch = payload.get("completed_at")
                    try:
                        if (
                            isinstance(completed_at, str)
                            and isinstance(completed_epoch, (int, float))
                            and abs(
                                datetime.fromisoformat(completed_at.replace("Z", "+00:00")).timestamp()
                                - float(completed_epoch)
                            ) > 5
                        ):
                            continue  # forked parent-history completion, not this child turn
                    except ValueError:
                        pass
                    if completed_at == state.get("last_complete"):
                        continue
                    state["last_complete"] = completed_at
                    attrs = {
                        "nickname": state.get("nickname"),
                        "model": state.get("model"),
                        "effort": state.get("effort"),
                        "depth": state.get("depth"),
                        "started_at": state.get("started_at"),
                    }
                    duration_ms = payload.get("duration_ms")
                    if isinstance(duration_ms, (int, float)):
                        attrs["duration_seconds"] = max(0, float(duration_ms) / 1000)
                    try:
                        if "duration_seconds" not in attrs and isinstance(state.get("started_at"), str) and isinstance(completed_at, str):
                            started = datetime.fromisoformat(str(state["started_at"]).replace("Z", "+00:00"))
                            ended = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
                            attrs["duration_seconds"] = max(0, (ended - started).total_seconds())
                    except ValueError:
                        pass
                    event = TranscriptEvent(
                        TranscriptKind.SUBAGENT_END,
                        text=f"{state.get('name') or 'Subagent'} completed",
                        data={
                            "lifecycle": "subagent",
                            "status": "completed",
                            "engine": "codex",
                            "name": state.get("name") or "subagent",
                            "subagent_key": state.get("agent_path") or key,
                            **{k: v for k, v in attrs.items() if v is not None and v != ""},
                        },
                    )
                    if isinstance(completed_at, str):
                        try:
                            event = dataclasses.replace(
                                event, at=datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
                            )
                        except ValueError:
                            pass
                    self._sink.emit(event)

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
    # Fold any inline subagent events into child sessions now (the same
    # write-path step a headless harness run does), so the dashboard never has
    # to derive them on read.
    try:
        from archon_horizon.transcript.subagents import materialize_subagent_sessions

        materialize_subagent_sessions(transcript_path.parent)
    except Exception:
        pass
    return returncode


# ── seed prompts ─────────────────────────────────────────────────────────────
#
# Interactive sessions are *human-driven*, so we don't compose the full
# orchestrated agent prompt (which assumes a specific task/round). Instead we seed
# a short role brief that points the engine at the workspace, the docs, and the
# on-disk state, then let the human steer from there.

_DISCUSS_BRIEF = """\
You are the **discuss** companion for this Archon Horizon workspace — here to
talk with the human and do what they ask, nothing more.

Load the **`horizon`** skill first (`.claude/skills/horizon/SKILL.md`): it
explains the workspace layout, the `horizon` CLI, and where live state lives
(`.archon-horizon/` — runs, tasks, inbox, roadmap, blueprints). For questions
about Archon Horizon itself, read the installed package's README/docs/source.

Rules of engagement:
- Only *modify* anything when the human explicitly asks. Otherwise explain,
  propose, and wait.
- Be concrete: cite exact files, task ids, and commands.
- Start by briefly greeting the human with a short status summary (recent runs,
  open tasks/inbox), then ask what they'd like to do."""


def discuss_prompt(root: Path) -> str:
    """Seed prompt for the `horizon discuss` companion agent."""
    return f"You are in an **Archon Horizon** workspace at `{root}`.\n\n{_DISCUSS_BRIEF}\n"


def horizon_seed_prompt(
    root: Path, focus: tuple[str, ...] = (), *, resuming: bool = False
) -> str:
    """The interactive seed: the only instruction is to load the `horizon` skill
    and wait for the user — no composed role brief, no pushed policy. The skill
    (editable at ``.claude/skills/horizon/SKILL.md``) carries the orientation and
    conventions; everything else the agent pulls on demand.

    ``focus`` (task ids / projects / files the human targeted) is passed through
    so the agent can start there after loading the skill; ``resuming`` frames the
    session as continuing an earlier engine conversation."""
    lines = [
        f"You are in an **Archon Horizon** workspace at `{root}`.",
        "",
        "Load the **`horizon`** skill — it explains where the state lives, the tools, "
        "and the conventions for this workspace. Do that first.",
    ]
    items = ", ".join(f"`{f}`" for f in focus)
    if resuming:
        target = f" on {items}" if items else ""
        lines += [
            "",
            f"You are RESUMING an earlier interactive session{target}. If that "
            "conversation is already in context, pick up where you left off; "
            "otherwise orient from the workspace state (recent ledger history and "
            "the previous session's report, as the skill describes). Briefly say "
            "where things stand and what you propose next.",
        ]
    elif focus:
        lines += [
            "",
            f"The user launched this session focused on: {items}. After loading the "
            "skill, orient on that and propose a first step.",
        ]
    lines += [
        "",
        "Then briefly greet the user and wait for their instructions — they are driving.",
    ]
    return "\n".join(lines) + "\n"
