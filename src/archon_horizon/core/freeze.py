"""Freeze model and the deterministic write gate.

Freeze is first-class and enforced in code, never by prompt. A frozen
target means: agents may read and report, but a Horizon task whose declared
write set touches it must be refused *before* dispatch. The orchestrator
calls :func:`frozen_violations` against each task's
:class:`~archon_horizon.core.tasks.WriteSet`; a non-empty result blocks the
task unless an explicit override is recorded in the event log.

This module is pure: glob matching only, no I/O.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from enum import StrEnum

from .tasks import WriteSet
from .types import Metadata


class FreezeLevel(StrEnum):
    WORKSPACE = "workspace"
    PROJECT = "project"
    FILE = "file"
    DECLARATION = "declaration"
    BLUEPRINT_NODE = "blueprint-node"
    AGENT = "agent"


@dataclass(frozen=True, slots=True)
class FreezeRule:
    """One frozen target.

    ``pattern`` is interpreted by ``level``: a project name, a posix file
    glob, a Lean declaration name, a blueprint node id, or an agent name.
    """

    level: FreezeLevel
    pattern: str
    reason: str = ""
    metadata: Metadata = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FreezeSet:
    rules: tuple[FreezeRule, ...] = ()

    def _by_level(self, level: FreezeLevel) -> tuple[FreezeRule, ...]:
        return tuple(rule for rule in self.rules if rule.level is level)

    def workspace_rule(self) -> FreezeRule | None:
        rules = self._by_level(FreezeLevel.WORKSPACE)
        return rules[0] if rules else None

    def project_rule(self, project: str) -> FreezeRule | None:
        for rule in self._by_level(FreezeLevel.PROJECT):
            if rule.pattern == project:
                return rule
        return None

    def file_rule(self, file: str) -> FreezeRule | None:
        for rule in self._by_level(FreezeLevel.FILE):
            if file == rule.pattern or fnmatch.fnmatch(file, rule.pattern):
                return rule
        return None

    def agent_rule(self, agent: str) -> FreezeRule | None:
        for rule in self._by_level(FreezeLevel.AGENT):
            if rule.pattern in (agent, "*"):
                return rule
        return None


def frozen_violations(write_set: WriteSet, freeze: FreezeSet) -> tuple[FreezeRule, ...]:
    """Return the freeze rules a write set would violate (empty == allowed).

    A workspace-level freeze blocks any write. Otherwise each declared
    project and file is checked against project- and file-level rules.
    Declaration- and node-level freezes are advisory here (the write set
    does not carry that granularity) and are enforced by the reviewer.
    """
    touches_anything = write_set.workspace or bool(write_set.projects) or bool(write_set.files)

    violations: list[FreezeRule] = []
    workspace_rule = freeze.workspace_rule()
    if workspace_rule is not None and touches_anything:
        violations.append(workspace_rule)

    for project in write_set.projects:
        rule = freeze.project_rule(project)
        if rule is not None:
            violations.append(rule)

    for file in write_set.files:
        rule = freeze.file_rule(file)
        if rule is not None:
            violations.append(rule)

    return tuple(violations)
