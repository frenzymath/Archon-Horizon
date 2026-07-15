"""Prompt composition for the Ground and Horizon agents.

The prompts keep Archon Horizon's ontology small and human-first. There is no
machine-readable output contract: agents *act* during the run (editing
blueprints / roadmap / memory directly, and using the ``horizon`` CLI for
inbox actions), then finish with a short prose report. The orchestrator
reconstructs what changed by reading state back from disk, not by parsing the
agent's text. These functions are pure data-to-string transforms so a harness
can swap Claude Code, Codex, or any command-line agent without changing agent
code.
"""

from __future__ import annotations

from pathlib import Path

from archon_horizon.core.inbox import InboxItem, InboxKind
from archon_horizon.core.roadmap import Roadmap
from archon_horizon.core.tasks import HorizonTask, WriteSet
from archon_horizon.core.workspace import Workspace
from archon_horizon.skills.registry import available_skills
from archon_horizon.subagents.registry import descriptor_summary

from .base import HorizonContext

_MAX_INBOX_ITEMS_IN_PROMPT = 10
_MAX_INBOX_BODY_CHARS = 700

# Bundled default prompt bodies (the static role/policy prose). A workspace can
# override any of them by dropping its own edited copy at
# ``<state_dir>/prompts/<name>.md`` — `horizon init`/`update` installs the bundled
# defaults there, and the composer prefers the workspace copy so users can tune the
# agents' instructions without touching code. Only the STATIC prose lives here; the
# dynamic sections (roadmap, inbox, task, write scope, skills, …) are still injected
# by the compose functions below.
PROMPT_TEMPLATE_DIR = Path(__file__).parent / "prompt_templates"


def workspace_prompt_path(workspace: Workspace, name: str) -> Path:
    """Where a workspace keeps its editable override of prompt body ``name``."""
    return workspace.state_path / "prompts" / f"{name}.md"


def bundled_prompt_names() -> tuple[str, ...]:
    """The prompt bodies shipped with the package (installed into a workspace)."""
    return tuple(sorted(p.stem for p in PROMPT_TEMPLATE_DIR.glob("*.md")))


def install_prompts(root: Path, *, state_dir: str = ".archon-horizon", overwrite=None) -> list[str]:
    """Copy the bundled prompt bodies into ``<root>/<state_dir>/prompts/<name>.md``
    so a human can edit the agents' role/policy prose. Same keep-vs-overwrite
    semantics as ``install_skills``: an identical destination is skipped; a differing
    one is overwritten unless the ``overwrite(name, dest, new_text)`` callback returns
    False. Returns the names actually written."""
    installed: list[str] = []
    dest_dir = root / state_dir / "prompts"
    for src in sorted(PROMPT_TEMPLATE_DIR.glob("*.md")):
        dest = dest_dir / src.name
        new_text = src.read_text("utf-8")
        if dest.exists():
            if dest.read_text("utf-8") == new_text:
                continue
            if overwrite is not None and not overwrite(src.stem, dest, new_text):
                continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest.write_text(new_text, "utf-8")
        installed.append(src.stem)
    return installed


def _load_prompt_body(workspace: Workspace, name: str) -> str:
    """The static prompt body for ``name``, preferring the workspace override over
    the bundled default so a human edit to ``<state_dir>/prompts/<name>.md`` takes
    effect on the next run. Falls back to the bundled copy if the override is
    missing or empty."""
    override = workspace_prompt_path(workspace, name)
    try:
        if override.is_file():
            text = override.read_text("utf-8")
            if text.strip():
                return text.rstrip()
    except OSError:
        pass
    return (PROMPT_TEMPLATE_DIR / f"{name}.md").read_text("utf-8").rstrip()


def _state_file(context: HorizonContext, name: str) -> str:
    return f"{context.workspace.state_dir.as_posix()}/{name}"


def _write_scope(write_domain: tuple[str, ...]) -> str:
    if not write_domain:
        return "Write scope: unrestricted (no domain configured). Read scope: the entire workspace."
    allowed = "\n".join(f"- {glob}" for glob in write_domain)
    return (
        "Write scope — you may edit ONLY paths matching these globs; treat "
        "everything else as read-only:\n"
        f"{allowed}\n"
        "Read scope: the entire workspace (cross-project reads are allowed)."
    )


def _skills_block() -> str:
    """List on-demand capability skills, keeping their detail out of the prompt."""
    skills = available_skills()
    if not skills:
        return ""
    lines = "\n".join(f"- {skill.name} — {skill.description}" for skill in skills)
    return (
        "# Skills\n"
        "Invoke the Horizon CLI via the `$HORIZON_BIN` env var (an absolute path), e.g. "
        "`\"$HORIZON_BIN\" inbox list --json` — a bare `horizon` may not be on your shell's PATH.\n"
        "Most `horizon` commands accept `--json`; use it for AI-friendly output when reading state or ids.\n"
        "Capability know-how lives in skill files under the workspace `.claude/skills/<name>/SKILL.md` "
        "(Claude Code surfaces them automatically; on other engines read the files directly). Consult "
        "the relevant one before acting (exact inbox verbs, lean checks, blueprint conventions, the dependency DAG):\n"
        f"{lines}"
    )


def _report_guidance(role: str) -> str:
    # Horizon owns its task's terminal status; the machine only ever writes
    # queued/running, so `done` is never inferred from the report or a clean exit —
    # the agent must record it explicitly. See the task-status skill.
    status_line = (
        "You OWN your task's status: the machine only ever leaves it queued/running, "
        "so nothing marks it done for you and the report is NOT parsed for completion. "
        "Only when the assigned objective is FULLY complete (not merely mostly), run "
        "`\"$HORIZON_BIN\" task set <task_id> --status done`; use `--status blocked` or "
        "`--status failed` if you are genuinely stuck. If it is only partly advanced, "
        "set nothing — the task returns to the queue for the next round. Leave a "
        "`\"$HORIZON_BIN\" task comment <task_id> --body …` whenever you hit a "
        "significant step or change the status. "
        if role == "Horizon" else ""
    )
    return (
        "Finish with a brief run-local report. Recommended sections: `## Summary`, "
        "`## Progress`, `## Issues`, `## Why I stopped`, and `## Next`; add or rename "
        "other sections if another shape is clearer, but keep `## Progress` and "
        "`## Why I stopped`. In `## Progress`, use only inline `-` bullets, one per "
        "relevant file or target, e.g. `- FileA.lean: 4 sorries -> 3 sorries; closed "
        "the base case.` or `- FileB.lean: No change because dependency X is blocked.` "
        "Do not use nested bullets or wrapped multi-line bullets there. "
        "In `## Why I stopped`, state plainly whether the assigned objective is fully "
        "complete, partly advanced, or blocked, and why. "
        + status_line +
        "If there is a plausible next action inside the session's scope and budget, "
        "take it before stopping; a clean commit or green partial check is not by "
        "itself a reason to stop. Prefer short bullets, and keep each bullet around "
        "20 words or fewer. Always mention bugs, build failures, broken proofs, "
        "suspicious code issues, blocked dependencies, unresolved assumptions, and "
        f"checks that failed or were not run. For this {role} session, make the report "
        "complete enough that a human or Ground can understand the session before "
        "opening raw logs."
    )


def _pending_work_guidance() -> str:
    return (
        "This is a ONE-SHOT headless session: when you produce your final report the "
        "session ENDS and you will NOT be re-invoked. Nothing calls you back when a "
        "background job finishes, and any background process you started is killed "
        "when the session exits. So you cannot 'kick off a build and wait to be "
        "notified' — that notification never comes here, and the build dies.\n"
        "Therefore: if a build, subagent, monitor, or shell command produces a result "
        "your conclusion depends on, you MUST get that result WITHIN this session — "
        "run it in the FOREGROUND and block on it (read its exit code), or actively "
        "poll until it has definitively finished, before writing the report. A heavy "
        "`lake build` can take many minutes; budget for that and wait it out. If it "
        "genuinely cannot finish in time, do NOT imply success: record the unfinished "
        "build under `## Issues`, state what's still unknown, and leave the explicit "
        "next action — never write a conclusion that assumes a pending result."
    )

def _roadmap_lines(roadmap: Roadmap) -> str:
    if not roadmap.items:
        return "(roadmap is empty)"
    lines = []
    for item in roadmap.items:
        projects = ", ".join(item.projects) or "workspace"
        deps = f" deps={','.join(item.depends_on)}" if item.depends_on else ""
        refs = f" tasks={','.join(item.task_refs)}" if item.task_refs else ""
        summary = f" - {item.summary}" if item.summary else ""
        lines.append(
            f"- {item.id} [{item.status}/{item.kind}/{item.priority}] ({projects}) "
            f"{item.title}{summary}{deps}{refs}"
        )
    return "\n".join(lines)


def _scope_suffix(item: InboxItem) -> str:
    scope = []
    projects = item.scope.targets("projects")
    files = item.scope.targets("files")
    declarations = item.scope.targets("declarations")
    if projects:
        scope.append("projects=" + ",".join(projects))
    if files:
        scope.append("files=" + ",".join(files))
    if declarations:
        scope.append("decls=" + ",".join(declarations))
    return f" ({', '.join(scope)})" if scope else ""


def _compact_text(text: str, limit: int = _MAX_INBOX_BODY_CHARS) -> str:
    normalized = " ".join(text.strip().split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _inbox_priority(item: InboxItem) -> tuple[int, str]:
    """Stable prompt ordering: urgent durable coordination first."""
    kind_rank = {
        InboxKind.ISSUE: 0,
        InboxKind.HINT: 1,
    }.get(item.kind, 2)
    audience_rank = 0 if item.audience in ("horizon", "ground") else 1 if item.audience else 2
    return (kind_rank, audience_rank, item.id)


def _inbox_lines(items: tuple[InboxItem, ...]) -> str:
    # Protection and memory items get their own sections, not the general list.
    items = tuple(
        sorted(
            (i for i in items if i.kind not in (InboxKind.PROTECTION, InboxKind.MEMORY)),
            key=_inbox_priority,
        )
    )
    if not items:
        return "(no accepted inbox items)"
    lines = []
    shown = items[:_MAX_INBOX_ITEMS_IN_PROMPT]
    for item in shown:
        meta = []
        if item.author:
            meta.append(f"by={item.author}")
        if item.audience:
            meta.append(f"to={item.audience}")
        if item.labels:
            meta.append("labels=" + ",".join(item.labels))
        scope = _scope_suffix(item).strip(" ()")
        if scope:
            meta.append(scope)
        suffix = f" ({', '.join(meta)})" if meta else ""
        body = _compact_text(item.body)
        lines.append(f"- {item.id} {item.kind}{suffix}: {body}")
    hidden = len(items) - len(shown)
    if hidden > 0:
        lines.append(
            f"- … {hidden} more accepted inbox item(s) omitted from the prompt; "
            "use `\"$HORIZON_BIN\" inbox list --json` if needed."
        )
    return "\n".join(lines)


def _protected_block(items: tuple[InboxItem, ...]) -> str:
    """Render protection items (the soft freeze) as an explicit do-not-modify list."""
    prot = [i for i in items if i.kind is InboxKind.PROTECTION]
    if not prot:
        return ""
    lines = "\n".join(
        f"- {item.id}{_scope_suffix(item)}: {item.body.strip()}" for item in prot
    )
    return (
        "# Protected standing constraints — leave these unchanged, including semantic "
        "constraints like a declaration's signature:\n"
        f"{lines}"
    )


def _section(block: str) -> str:
    """A rendered block followed by a separator, or nothing when empty."""
    return f"{block}\n\n" if block else ""


def _workspace_lines(workspace: Workspace) -> str:
    if not workspace.projects:
        return "(no projects configured)"
    out = []
    for name, project in sorted(workspace.projects.items()):
        bp = f", blueprint={project.blueprint_path}" if project.blueprint_path else ""
        build = f", build={project.build_command}" if project.build_command else ""
        out.append(f"- {name}: path={project.path}, type={project.type}{bp}{build}")
    return "\n".join(out)


def _write_set_lines(write_set: WriteSet) -> str:
    parts = []
    if write_set.workspace:
        parts.append("workspace")
    if write_set.projects:
        parts.append("projects=" + ",".join(write_set.projects))
    if write_set.files:
        parts.append("files=" + ",".join(write_set.files))
    return "; ".join(parts)


def _horizon_projects_line(context: HorizonContext) -> str:
    """Render the project(s) a task covers — a task may span several."""
    task = context.task
    names = task.projects or ((task.project,) if task.project else ())
    if len(names) <= 1:
        name = names[0] if names else task.project
        return f"Project: {name} at {context.workspace.project_path(name)}"
    rows = "\n".join(f"  - {n} at {context.workspace.project_path(n)}" for n in names)
    return f"Projects (this task spans several — work across all of them):\n{rows}"


def _task_block(task: HorizonTask) -> str:
    refs = []
    if task.roadmap_refs:
        refs.append("roadmap=" + ",".join(task.roadmap_refs))
    if task.inbox_refs:
        refs.append("inbox=" + ",".join(task.inbox_refs))
    ref_line = f"\nReferences: {'; '.join(refs)}" if refs else ""
    suggested = _write_set_lines(task.write_set)
    scope_line = f"\nSuggested scope: {suggested}" if suggested else ""
    title = task.title or task.id
    body = task.explanation or task.objective
    return (
        f"Task: {task.id}\nTitle: {title}\nProject: {task.project}{scope_line}{ref_line}\n\n"
        f"{body.strip()}"
    )


def _subagent_catalog(context: HorizonContext) -> str:
    directory = context.workspace.state_path / "subagents"
    summary = descriptor_summary(directory)
    return (
        "You have native subagents installed for your engine, compiled at run "
        "start from the descriptors in `.archon-horizon/subagents/<name>.md` "
        "(Claude reads `.claude/agents/`, Codex reads `.codex/agents/`):\n"
        f"{summary}\n\n"
        "Delegate by spawning a subagent **by name** through your engine's own "
        "subagent mechanism, giving it a focused directive: the slice/scope and "
        "the project it applies to. Spawn several in parallel when the work "
        "divides cleanly, then wait for and reconcile their reports. Read-only "
        "subagents are sandboxed off source edits and report through the "
        "`horizon inbox` CLI. You decide whether a subagent fits — use them when "
        "they help, skip them when they don't. To add or change a subagent, edit "
        "its descriptor under `.archon-horizon/subagents/` — it recompiles on the "
        "next run.\n\n"
        "YOU own the model choice for every piece of delegated work — a subagent "
        "spawned by name, a bare Task/Agent spawn, or a Workflow/orchestration if "
        "your engine offers one (including any automatic fan-out your session mode "
        "does for you). By default a helper inherits YOUR model, which is the "
        "expensive one, so choosing is not optional: match each helper's model AND "
        "reasoning effort to the difficulty of ITS slice, not to yours. Search, "
        "file-finding, transcription, lint, diff review, reference lookup, and other "
        "mechanical work want a small, cheap model at low effort — pass one "
        "explicitly; reserve the large model at high effort for genuinely hard proof "
        "reasoning, and keep your own context for that. Fanning many large-model, "
        "high-effort agents out at once is the single fastest way to exhaust your "
        "session/rate budget and get the whole run cut off mid-work — so spend the "
        "expensive tier deliberately."
    )


def compose_horizon_prompt(context: HorizonContext) -> str:
    """Prompt for one autonomous long-horizon formalization attempt.

    The static role/policy prose is loaded from ``horizon.md`` (a workspace override
    at ``<state_dir>/prompts/horizon.md`` wins over the bundled default); the dynamic
    context sections are injected here."""
    return (
        f"{_load_prompt_body(context.workspace, 'horizon')}\n\n"
        f"{_skills_block()}\n\n"
        f"Workspace: {context.workspace.name}\n"
        f"{_horizon_projects_line(context)}\n"
        f"{_write_scope(context.write_domain)}\n"
        f"Log/artifact directory: {context.log_dir or '(none)'}\n\n"
        f"# Subagents\n{_subagent_catalog(context)}\n\n"
        f"# Task focus\n{_task_block(context.task)}\n\n"
        f"# Roadmap slice\n{_roadmap_lines(context.roadmap)}\n\n"
        f"{_section(_protected_block(context.accepted_inbox))}"
        f"# Opened inbox\n{_inbox_lines(context.accepted_inbox)}\n\n"
        f"# Memory\n{context.memory.strip() or '(empty)'}\n\n"
        f"{_pending_work_guidance()}\n\n"
        f"{_report_guidance('Horizon')}"
    )
