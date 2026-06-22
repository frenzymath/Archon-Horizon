"""Prompt composition for the informal and Horizon agents.

The prompts intentionally keep Archon Horizon's ontology small while borrowing
what worked in Archon: explicit role boundaries, a tight context packet,
blueprint/DAG discipline, protected-state language, and a machine-readable
output contract at the end. They are pure data-to-string transforms so a
harness can swap Claude Code, Codex, or any command-line agent without changing
agent code.
"""

from __future__ import annotations

from archon_horizon.core.inbox import InboxItem
from archon_horizon.core.roadmap import Roadmap
from archon_horizon.core.tasks import HorizonTask, WriteSet
from archon_horizon.core.workspace import Workspace
from archon_horizon.subagents.registry import descriptor_summary

from .base import HorizonContext, InformalContext


_SCHEMA = """
# Required structured output contract

End your response with one fenced json object. Prose above the block is the human report.
If there are no structured changes, emit an empty object.

```json
{
  "memory": "optional replacement memory text",
  "roadmap": {
    "items": [
      {
        "id": "R-0001",
        "title": "Human readable milestone",
        "projects": ["project-name"],
        "summary": "short status/context",
        "status": "pending|active|blocked|done|rejected",
        "kind": "proof|blueprint|refactor|workspace|report",
        "priority": "low|normal|high",
        "depends_on": ["R-0000"],
        "inbox_refs": ["I-0001"],
        "task_refs": ["T-0001"]
      }
    ]
  },
  "tasks": [
    {
      "project": "project-name",
      "objective": "one precise Horizon task",
      "write_set": {"files": ["Relative/File.lean"], "projects": [], "workspace": false},
      "roadmap_refs": ["R-0001"],
      "inbox_refs": ["I-0001"]
    }
  ],
  "proposals": [
    {"title": "Proposal title", "body": "what should change", "project": "optional-project", "metadata": {}}
  ],
  "local_issues": [
    {"kind": "issue|question|blocker|proposal|hint", "body": "triage note", "project": "optional-project"}
  ]
}
```
"""


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


def _inbox_lines(items: tuple[InboxItem, ...]) -> str:
    if not items:
        return "(no accepted inbox items)"
    lines = []
    for item in items:
        scope = []
        if item.scope.project:
            scope.append(f"project={item.scope.project}")
        if item.scope.file:
            scope.append(f"file={item.scope.file}")
        if item.scope.declaration:
            scope.append(f"decl={item.scope.declaration}")
        suffix = f" ({', '.join(scope)})" if scope else ""
        body = item.body.strip().replace("\n", "\n  ")
        lines.append(f"- {item.id} {item.kind}{suffix}: {body}")
    return "\n".join(lines)


def _workspace_lines(workspace: Workspace) -> str:
    if not workspace.projects:
        return "(no projects configured)"
    out = []
    for name, project in sorted(workspace.projects.items()):
        bp = f", blueprint={project.blueprint_path}" if project.blueprint_path else ""
        build = f", build={project.build_command}" if project.build_command else ""
        out.append(f"- {name}: path={project.path}, type={project.type}{bp}{build}")
    return "\n".join(out)


def _focus_line(context: InformalContext) -> str:
    parts: list[str] = []
    if context.focus.projects:
        parts.append("projects=" + ",".join(context.focus.projects))
    if context.focus.task:
        parts.append("task=" + context.focus.task)
    if context.focus.proposal:
        parts.append("proposal=" + context.focus.proposal)
    return ", ".join(parts) if parts else "workspace-wide"


def _write_set_lines(write_set: WriteSet) -> str:
    parts = []
    if write_set.workspace:
        parts.append("workspace")
    if write_set.projects:
        parts.append("projects=" + ",".join(write_set.projects))
    if write_set.files:
        parts.append("files=" + ",".join(write_set.files))
    return "; ".join(parts) if parts else "unknown (scheduler will lock the project pessimistically)"


def _task_block(task: HorizonTask) -> str:
    refs = []
    if task.roadmap_refs:
        refs.append("roadmap=" + ",".join(task.roadmap_refs))
    if task.inbox_refs:
        refs.append("inbox=" + ",".join(task.inbox_refs))
    ref_line = f"\nReferences: {'; '.join(refs)}" if refs else ""
    return (
        f"# Task {task.id or '(unassigned)'}\n"
        f"Project: {task.project}\n"
        f"Write set: {_write_set_lines(task.write_set)}{ref_line}\n\n"
        f"{task.objective.strip()}"
    )


def _subagent_catalog(context: InformalContext) -> str:
    directory = context.workspace.state_path / "subagents"
    summary = descriptor_summary(directory)
    parent = context.log_dir or "<this session log dir>"
    return (
        "Descriptor subagents live in `.archon-horizon/subagents/<name>.md`.\n"
        f"{summary}\n\n"
        "Invoke a subagent with a blocking Bash call:\n"
        "```bash\n"
        "python3 .claude/tools/horizon-subagent.py \\\n"
        "  --name <name> \\\n"
        "  --slug <kebab-slug> \\\n"
        "  --directive-file <path-to-directive.md> \\\n"
        f"  --parent-log-dir {parent} \\\n"
        "  --write-domain '<glob-or-path>'\n"
        "```\n"
        "Before dispatching, write a focused Markdown directive file under the current log directory. "
        "The child subagent will create a nested transcript under `subagents/`."
    )


def compose_informal_prompt(context: InformalContext) -> str:
    """Prompt for the human-facing planning/blueprint/roadmap agent."""
    return (
        "You are Archon Horizon's informal agent. You own and maintain the human-readable "
        "state. You must ALWAYS ensure that the blueprints are perfectly accurate and that "
        "the roadmap and memory are perfectly updated to reflect the true state of the project. "
        "You do not do long Lean proof search yourself; you prepare precise Horizon tasks and "
        "keep the collaboration legible.\n\n"
        "Operational rules:\n"
        "- Treat accepted inbox items as input at this sync boundary only.\n"
        "- Proactively invoke subagents (like blueprint-reviewer) to ensure blueprints are perfect.\n"
        "- Prefer dependency-correct blueprint and roadmap progress over large vague tasks.\n"
        "- If a task needs Lean work, create one focused Horizon task with a clear write_set.\n"
        "- If information is missing, raise a local issue or proposal instead of guessing.\n"
        "- Keep durable memory short: facts, conventions, dead ends, and project invariants.\n"
        "- Preserve frozen/protected state. If an edit would violate it, report a blocker.\n\n"
        f"Workspace: {context.workspace.name}\n"
        f"Run: {context.run.id or '(new run)'}; focus: {_focus_line(context)}\n"
        f"Log directory for this session: {context.log_dir or '(none)'}\n\n"
        f"# Projects\n{_workspace_lines(context.workspace)}\n\n"
        f"# Roadmap\n{_roadmap_lines(context.roadmap)}\n\n"
        f"# Blueprint / DAG summary\n{context.blueprint_summary or '(no blueprint summary)'}\n\n"
        f"# Subagents\n{_subagent_catalog(context)}\n\n"
        f"# Accepted inbox\n{_inbox_lines(context.accepted_inbox)}\n\n"
        f"# Memory\n{context.memory.strip() or '(empty)'}\n"
        + _SCHEMA
    )


def compose_horizon_prompt(context: HorizonContext) -> str:
    """Prompt for one autonomous long-horizon formalization attempt."""
    return (
        "You are Archon Horizon's Horizon agent. Complete exactly one assigned "
        "task in the project worktree. You may edit Lean and nearby blueprint "
        "material only when it is necessary for the task and inside the write set.\n\n"
        "Operational rules inspired by Archon:\n"
        "- Start from the task, sliced roadmap, accepted hints, and project files.\n"
        "- Read the relevant blueprint/proof sketch before changing Lean.\n"
        "- Run the project's build/check command when available, or the narrowest useful Lean check.\n"
        "- Repair failures you introduce. Do not mask hard obligations with new sorries unless the task explicitly allows it.\n"
        "- Keep the final report short: changed files, proof status, checks run, blockers.\n"
        "- Do not polish public roadmap prose; the informal agent will translate your result.\n\n"
        f"Workspace: {context.workspace.name}\n"
        f"Project root: {context.workspace.project_path(context.task.project)}\n"
        f"Log/artifact directory: {context.log_dir or '(none)'}\n\n"
        f"{_task_block(context.task)}\n\n"
        f"# Roadmap slice\n{_roadmap_lines(context.roadmap)}\n\n"
        f"# Accepted inbox\n{_inbox_lines(context.accepted_inbox)}\n\n"
        f"# Memory\n{context.memory.strip() or '(empty)'}\n"
    )
