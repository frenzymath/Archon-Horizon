"""Subagent contracts and descriptor-driven harness subagents.

Archon Horizon follows Archon's robust shape here: focused helper agents are
described by Markdown files with YAML frontmatter, then launched through the
same harness/logging path as any other agent. Deterministic checks still
implement :class:`Subagent` directly, but descriptor subagents are the default
extension point.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent

from archon_horizon.core.inbox import InboxDraft
from archon_horizon.core.types import Metadata
from archon_horizon.core.workspace import Workspace
from archon_horizon.harnesses.base import Harness, HarnessRequest


@dataclass(frozen=True, slots=True)
class SubagentDescriptor:
    name: str
    description: str = ""
    harness: str | None = None
    prompt_body: str = ""
    write_domain: str | None = None
    read_only: bool = False
    default_enabled: bool = True
    # Engine-agnostic model selection for the compiled native subagent.
    # ``model`` is an explicit override (passed through literally); ``tier`` is a
    # symbolic size (small/medium/big) resolved per-harness. Neither set → the
    # native subagent omits ``model`` and inherits the parent session.
    tier: str | None = None
    model: str | None = None
    source_path: Path | None = None


@dataclass(frozen=True, slots=True)
class SubagentContext:
    workspace: Workspace
    log_dir: Path | None = None
    directive: str = ""
    slug: str | None = None
    write_domain: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SubagentResult:
    ok: bool = True
    report: str = ""
    issues: tuple[InboxDraft, ...] = ()
    data: Metadata = field(default_factory=dict)


class Subagent(ABC):
    name: str

    @abstractmethod
    def run(self, context: SubagentContext) -> SubagentResult:
        """Analyze and report. Must not mutate workspace state."""


class DescriptorSubagent(Subagent):
    """Harness-backed subagent loaded from ``.md`` descriptor instructions."""

    def __init__(self, descriptor: SubagentDescriptor, harness: Harness) -> None:
        self.descriptor = descriptor
        self.name = descriptor.name
        self.harness = harness

    def prompt(
        self,
        context: SubagentContext,
        *,
        directive: str,
        slug: str | None = None,
        write_domain: tuple[str, ...] = (),
    ) -> str:
        log_dir = context.log_dir
        report_path = log_dir / "report.md" if log_dir is not None else None
        read_only = (
            "\nYou are read-only on Lean/blueprint/reference SOURCE files — do not "
            "edit them. You may still write your report, and use the `horizon inbox` "
            "CLI to file issues/memory; those are how you act.\n"
            if self.descriptor.read_only else ""
        )
        domain = "\n".join(f"- {d}" for d in write_domain) or self.descriptor.write_domain or "(unspecified)"
        return dedent(f"""\
            You are the {self.name} subagent for Archon Horizon workspace '{context.workspace.name}'.
            Workspace root: {context.workspace.root}
            State directory: {context.workspace.state_path}
            Slug: {slug or self.name}
            Log/artifact directory: {log_dir or '(none)'}
            Report path: {report_path or '(return your report as final text)'}

            Declared write domain:
            {domain}
            {read_only}
            # Local tools & layout
            You are launched at the workspace root (your cwd). Per-project files —
            the Lean source and `blueprint/` — live under the directory of the
            project you are assigned (named in your directive or write domain),
            not at the workspace root; resolve those relative paths there.
            `references/` is shared and lives at the WORKSPACE ROOT (one library
            for all projects), indexed by `references/manifest.yaml`. The
            blueprint dependency DAG for a project is the generated JSON
            at `.archon-horizon/blueprints/<project>.json`. Tool and format know-how
            is documented as skills under `.claude/skills/` (e.g. `leandag`,
            `blueprint-conventions`, `lean-check`, `leansearch`, `horizon-inbox`,
            `project-git`, `references`); read the relevant skill instead of guessing how a tool
            or format works. In particular, a project has NO `.git` at its root —
            read the `project-git` skill before running `git diff`/`git log`.

            # Subagent instructions
            {self.descriptor.prompt_body.strip()}

            # Directive
            {directive.strip()}

            Report back concisely in whatever structure best fits — lead with the
            outcome, keep it short, and file inbox items for anything the Ground
            agent must act on. Write the report to the report path if one is
            provided, and also return it as your final response.
        """)

    def run(self, context: SubagentContext) -> SubagentResult:
        result = self.harness.run(HarnessRequest(
            prompt=self.prompt(
                context,
                directive=context.directive or "Inspect the workspace and report findings.",
                slug=context.slug,
                write_domain=context.write_domain,
            ),
            cwd=context.workspace.root,
            artifact_dir=context.log_dir,
        ))
        report = result.text
        report_path = context.log_dir / "report.md" if context.log_dir is not None else None
        if report_path is not None and report_path.exists():
            report = report_path.read_text("utf-8")
        return SubagentResult(
            ok=result.ok,
            report=report,
            data={
                "descriptor": self.descriptor.name,
                "harness": self.harness.name,
                "artifact_refs": result.artifact_refs,
                "usage": {
                    "tokens_in": result.usage.tokens_in,
                    "tokens_out": result.usage.tokens_out,
                    "cost_usd": result.usage.cost_usd,
                },
            },
        )
