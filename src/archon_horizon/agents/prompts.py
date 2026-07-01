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

from archon_horizon.core.inbox import InboxItem, InboxKind
from archon_horizon.core.roadmap import Roadmap
from archon_horizon.core.tasks import HorizonTask, WriteSet
from archon_horizon.core.workspace import Workspace
from archon_horizon.skills.registry import available_skills
from archon_horizon.subagents.registry import descriptor_summary

from .base import HorizonContext, GroundContext

_MAX_INBOX_ITEMS_IN_PROMPT = 10
_MAX_INBOX_BODY_CHARS = 700


def _state_file(context: GroundContext | HorizonContext, name: str) -> str:
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
    return (
        f"Finish with a brief run-local report. Recommended sections: `## Summary`, "
        "`## Progress`, `## Issues`, and `## Next`; add or rename sections if another "
        "shape is clearer. Prefer short bullets, and keep each bullet around 20 words "
        "or fewer. Always mention bugs, build failures, broken proofs, suspicious code "
        "issues, blocked dependencies, unresolved assumptions, and checks that failed "
        f"or were not run. For this {role} session, make the report complete enough "
        "that a human or Ground can understand the session before opening raw logs."
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
        "# Protected — DO NOT modify these; they are standing constraints (respect "
        "them, including semantic ones like a declaration's signature):\n"
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


def _focus_line(context: GroundContext) -> str:
    parts: list[str] = []
    if context.focus.projects:
        parts.append("projects=" + ",".join(context.focus.projects))
    if context.focus.task:
        parts.append("task=" + context.focus.task)
    if context.focus.tasks:
        parts.append("tasks=" + ",".join(context.focus.tasks))
    return ", ".join(parts) if parts else "workspace-wide"


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


def _ground_recommendation_block(context: HorizonContext) -> str:
    recommendation = str(context.metadata.get("ground_recommendation") or "").strip()
    if not recommendation:
        return ""
    return (
        "# Latest Ground recommendation\n"
        "This was saved as `recommendation.md` by the last Ground session; treat it as "
        "fresh guidance, not as a hard command if the live state contradicts it.\n"
        f"{recommendation}"
    )


def _subagent_catalog(context: GroundContext | HorizonContext) -> str:
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
        "they help, skip them when they don't. Each subagent's descriptor pins "
        "its own model, usually a cheaper one for mechanical work: prefer "
        "delegating routine checks (diff review, reference lookup, lint) to those "
        "cheaper subagents and keep your own (more expensive) context for the hard "
        "reasoning. To add or change a subagent, edit its descriptor under "
        "`.archon-horizon/subagents/` — it recompiles on the next run."
    )


def compose_ground_prompt(context: GroundContext) -> str:
    """Prompt for the human-facing planning/blueprint/roadmap agent."""
    return (
        "You are Archon Horizon's Ground agent: the keeper of the project's human-readable "
        "mind. You own three things and must leave each true to the project's real state "
        "before you finish:\n"
        f"- the blueprints (LaTeX-subset declarations + `\\uses` dependency edges),\n"
        "- the roadmap (the planning index that drives scheduling) — edit it ONLY via the "
        "`\"$HORIZON_BIN\" roadmap` CLI (`set`/`add`/`remove`, `comment <id> --author ground` to log a "
        "key advance; pass long prose with `--summary-file` and `--author` on add/set), NEVER by "
        "hand-editing the YAML, which corrupts it; the CLI quotes safely,\n"
        "- memory: durable facts/conventions/dead ends, kept as `memory` inbox items "
        "(`horizon inbox add --kind memory --author ground`); prune stale ones with `inbox complete`.\n"
        "You do NOT do long Lean proof search yourself. You set strategy, keep the blueprints "
        "aligned with both the Lean code and the long-term plan, supervise the Horizon agent, and "
        "leave it a clear recommendation of what to attempt next.\n\n"
        "The Horizon agent runs free and may leave the workspace messy or its reasoning shaky — "
        "you are its supervisor and the workspace's janitor, and you make it correct and tidy again.\n\n"
        "How to work:\n"
        "- Review the Horizon agent's latest work via its report and the project diff (see the "
        "`project-git` skill — projects have no root `.git`; diff them through their out-of-tree "
        "git). Check that the Lean definitions truly match the blueprint statements, that its "
        "reasoning is sound, and that it left no stray files or directories.\n"
        "- Reconcile blueprints, roadmap status, and memory with what actually changed. Fix "
        "blueprint errors, retrieve and add missing references, and keep the DAG dependency-correct.\n"
        "- When you find a flaw — a wrong proof, a Lean/blueprint mismatch, a dead end — raise an "
        "inbox item or comment rather than silently papering over it (see the horizon-inbox skill); use "
        "`--author ground` for inbox items/comments you create. ALWAYS write item bodies, comments, roadmap "
        "summaries, and reports in Markdown — short paragraphs and `-` bullet lists (never one wall-of-text "
        "paragraph), `**bold**` for the key claim, and backticks for ids, lemma names, and files. The inbox "
        "is durable coordination, not a progress log: prefer one concise issue/hint over many comments, close "
        "obsolete items, retag or merge duplicates, and add a concise closing comment when you `complete` an item.\n"
        "- When you make a change the human should know about — you restructured projects, took an "
        "unexpected direction, or hit something worth flagging — tell them with an `info` inbox item "
        "(`horizon inbox add --kind info --author ground`). It is purely a notice and never affects what runs.\n"
        "- You do not create tasks: tasks are the human's way to launch sessions. Organize pending work "
        "through the roadmap; you may `horizon task comment` but never add/edit/remove tasks.\n"
        "- Delegate to subagents to divide the work (blueprint review, diff analysis), preferring "
        "a smaller/cheaper model for mechanical checks.\n"
        "- Leave the next move as a concise recommendation: update the roadmap to account for "
        "the opened inbox, existing tasks, blueprint state, and current Lean state. A roadmap "
        "item marked active is advice, not a command; the Horizon agent may self-scope or choose "
        "a better route from the same context.\n"
        "- Treat the opened inbox below as input for this round only. Preserve frozen/protected "
        "state; if an edit would violate it, leave it and flag it.\n\n"
        "Blueprint constraints (legible and pure, but mathematically complete — see the "
        "blueprint-conventions skill):\n"
        "- One declaration's worth per node: a short statement + a COMPLETE proof (not a sketch). "
        "Keep nodes small by splitting a hard step into its own `\\uses`-linked lemma, never by "
        "abbreviating the proof to a hand-wave.\n"
        "- Pure mathematics only: no Lean tactics, no semi-Lean pseudocode, no project history. "
        "Do not restate Lean source as prose. Add a `\\uses` edge rather than re-explaining a "
        "dependency.\n"
        "- The roadmap is an index, not a journal: one line of summary per item; link, don't recount.\n"
        "- Memory holds only what is non-obvious and durable: conventions, invariants, dead ends.\n\n"
        f"{_skills_block()}\n\n"
        f"Workspace: {context.workspace.name}\n"
        f"Run: {context.run.id or '(new run)'}; focus: {_focus_line(context)}\n"
        f"{_write_scope(context.write_domain)}\n"
        f"Log directory for this session: {context.log_dir or '(none)'}\n\n"
        f"# Projects\n{_workspace_lines(context.workspace)}\n\n"
        f"# Roadmap\n{_roadmap_lines(context.roadmap)}\n\n"
        f"# Blueprint / DAG summary\n{context.blueprint_summary or '(no blueprint summary)'}\n\n"
        f"# Subagents\n{_subagent_catalog(context)}\n\n"
        f"{_section(_protected_block(context.accepted_inbox))}"
        f"# Opened inbox\n{_inbox_lines(context.accepted_inbox)}\n\n"
        f"# Memory\n{context.memory.strip() or '(empty)'}\n\n"
        f"{_pending_work_guidance()}\n\n"
        f"{_report_guidance('Ground')}"
    )


def compose_horizon_prompt(context: HorizonContext) -> str:
    """Prompt for one autonomous long-horizon formalization attempt."""
    return (
        "You are Archon Horizon's Horizon agent. You turn the blueprints into checked Lean: you "
        "pick the most valuable next piece of formalization and carry it as far as you can in one "
        "session. You self-scope from the recommendation, roadmap, and opened inbox below.\n\n"
        "You have broad freedom at the workspace level: experiment, change the strategy "
        "entirely, try alternative formulations, edit blueprint material when proving teaches you "
        "something, and — when a task calls for it — work across the several projects it spans, "
        "or create and merge projects. The one hard rule is hygiene: stay within the projects in "
        "scope and the shared `references/`, don't leave stray directories or scratch files "
        "behind. The Ground agent and its review subagents run after you to reconcile, check "
        "math/blueprint alignment, and tidy — so prefer making progress over being cautious.\n\n"
        "How to work:\n"
        "- Read the relevant blueprint node and proof sketch before touching Lean.\n"
        "- Run the project's build/check command (or the narrowest useful Lean check) and repair "
        "failures you introduce. Don't bury obligations under new `sorry`s unless a hint allows it.\n"
        "- Use the inbox to comment, close, create, and message other projects with `--author horizon` (see the "
        "horizon-inbox skill). ALWAYS write item bodies, comments, and reports in Markdown — short paragraphs "
        "and `-` bullet lists (never one wall-of-text paragraph), `**bold**` for the key claim, and backticks "
        "for ids, lemma names, and files. Give every item a short title and a non-empty description. Comment at "
        "each key advance, and add a concise closing comment (runs, LOC, files) before you `complete` an item. "
        "Treat `[persistent]` items as standing rules; close `[temporary]` ones once consumed.\n"
        "- Record durable dead ends as memory: `horizon inbox add --kind memory --to horizon "
        "--author horizon --body \"...\"`, so later sessions don't repeat them.\n"
        "- You may delegate to subagents when it helps (see the Subagents section): split a wide "
        "search across parallel read-only subagents, hand mechanical checks to a cheaper-model "
        "subagent, or pull references. You are free to decide whether they fit the task.\n\n"
        f"{_skills_block()}\n\n"
        f"Workspace: {context.workspace.name}\n"
        f"{_horizon_projects_line(context)}\n"
        f"{_write_scope(context.write_domain)}\n"
        f"Log/artifact directory: {context.log_dir or '(none)'}\n\n"
        f"# Subagents\n{_subagent_catalog(context)}\n\n"
        f"{_section(_ground_recommendation_block(context))}"
        f"# Recommended focus\n{_task_block(context.task)}\n\n"
        f"# Roadmap slice\n{_roadmap_lines(context.roadmap)}\n\n"
        f"{_section(_protected_block(context.accepted_inbox))}"
        f"# Opened inbox\n{_inbox_lines(context.accepted_inbox)}\n\n"
        f"# Memory\n{context.memory.strip() or '(empty)'}\n\n"
        f"{_pending_work_guidance()}\n\n"
        f"{_report_guidance('Horizon')}"
    )
