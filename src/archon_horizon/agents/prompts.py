"""The one automated-session prompt: a task directive plus "load the skill".

Archon Horizon pushes no role prose, policy, or workspace state through the
prompt. The `horizon` skill (installed at ``.claude/skills/horizon/SKILL.md``,
user-editable) is the contract — orientation, conventions, one-shot discipline,
report shape — and everything else (roadmap, inbox, memory, DAG) the agent
*pulls* on demand through the ``horizon`` CLI. The orchestrator reconstructs
what changed by reading state back from disk, never by parsing the agent's
text, so this composer stays a pure data-to-string transform any engine can
consume.
"""

from __future__ import annotations

from .base import HorizonContext


def _task_block(context: HorizonContext) -> str:
    task = context.task
    workspace = context.workspace
    names = task.projects or ((task.project,) if task.project else ())
    if names:
        projects = "\n".join(f"- {n} at {workspace.project_path(n)}" for n in names)
    else:
        projects = "- (workspace-wide — no single project)"
    refs = []
    if task.roadmap_refs:
        refs.append("roadmap=" + ",".join(task.roadmap_refs))
    if task.inbox_refs:
        refs.append("inbox=" + ",".join(task.inbox_refs))
    ref_line = f"\nReferences: {'; '.join(refs)}" if refs else ""
    title = task.title or task.id
    body = (task.explanation or task.objective or "").strip()
    body_block = f"\n\n{body}" if body else ""
    return (
        f"Task: {task.id}\n"
        f"Title: {title}\n"
        f"Project(s) — work across all of them:\n{projects}{ref_line}{body_block}"
    )


def _stale_skill_guard(context: HorizonContext) -> str:
    """Supply current minimum gates when an old editable skill is installed.

    Horizon deliberately does not overwrite workspace skill edits at run start.
    This small package-owned fallback prevents an old copy from silently losing
    critical LSP, delegation, and collection-hygiene behavior.
    """
    try:
        from archon_horizon.skills.registry import stale_skills

        names = stale_skills(context.workspace.root)
    except Exception:
        return ""
    if not names:
        return ""
    return (
        "\n\nThe installed workspace skills are stale ("
        + ", ".join(names)
        + "). Do not overwrite deliberate local edits automatically, but do not ignore "
        "the drift. Until it is reconciled: use the `lean-check` LSP loop before "
        "and after Lean edits; spawn and wait for a bounded native subagent on "
        "multi-file/multi-proof work; and dispatch `janitor` (`ground` fallback) "
        "for inbox/roadmap/task health warnings and before terminal completion."
    )


def horizon_task_prompt(context: HorizonContext) -> str:
    """Prompt for one automated Horizon session: directive + skill pointer."""
    skill_path = (
        context.workspace.root / ".claude" / "skills" / "horizon" / "SKILL.md"
    ).resolve()
    return (
        f"You are in an **Archon Horizon** workspace at `{context.workspace.root}`.\n\n"
        "Load the **`horizon`** skill FIRST. Read it at the ABSOLUTE path\n"
        f"`{skill_path}` (also exported as `$ARCHON_HORIZON_SKILL`) — do NOT use a\n"
        "relative path like `.claude/skills/horizon/SKILL.md`, which fails when your\n"
        "shell starts in a member project rather than the workspace root. The skill\n"
        "explains where the state lives, the tools, and this workspace's conventions\n"
        "(one-shot discipline, git commits, the final report)."
        f"{_stale_skill_guard(context)}\n\n"
        "# Task\n"
        f"{_task_block(context)}\n\n"
        "This is a headless, one-shot session: work the task's REAL objective as far\n"
        "as you genuinely can, committing progress as you go, then finish with the\n"
        "brief final report the skill specifies."
    )
