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
class FreezeViolation:
    """One concrete write target and the freeze rule that matched it."""

    target: str
    rule: FreezeRule


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

    def declaration_rule(self, declaration: str) -> FreezeRule | None:
        for rule in self._by_level(FreezeLevel.DECLARATION):
            if declaration == rule.pattern or fnmatch.fnmatch(declaration, rule.pattern):
                return rule
        return None

    def blueprint_node_rule(self, node: str) -> FreezeRule | None:
        for rule in self._by_level(FreezeLevel.BLUEPRINT_NODE):
            if node == rule.pattern or fnmatch.fnmatch(node, rule.pattern):
                return rule
        return None


def freeze_violation_details(
    write_set: WriteSet, freeze: FreezeSet
) -> tuple[FreezeViolation, ...]:
    """Return each concrete write target together with its matching rule.

    A workspace-level freeze blocks any write. Otherwise each declared
    project, file, Lean declaration, and blueprint node is checked against
    the corresponding freeze rules.
    """
    touches_anything = (
        write_set.workspace
        or bool(write_set.projects)
        or bool(write_set.files)
        or bool(write_set.declarations)
        or bool(write_set.blueprint_nodes)
    )

    violations: list[FreezeViolation] = []
    workspace_rule = freeze.workspace_rule()
    if workspace_rule is not None and touches_anything:
        violations.append(FreezeViolation(target="workspace", rule=workspace_rule))

    for project in write_set.projects:
        rule = freeze.project_rule(project)
        if rule is not None:
            violations.append(FreezeViolation(target=project, rule=rule))

    for file in write_set.files:
        rule = freeze.file_rule(file)
        if rule is not None:
            violations.append(FreezeViolation(target=file, rule=rule))

    for declaration in write_set.declarations:
        rule = freeze.declaration_rule(declaration)
        if rule is not None:
            violations.append(FreezeViolation(target=declaration, rule=rule))

    for node in write_set.blueprint_nodes:
        rule = freeze.blueprint_node_rule(node)
        if rule is not None:
            violations.append(FreezeViolation(target=node, rule=rule))

    return tuple(violations)


def frozen_violations(write_set: WriteSet, freeze: FreezeSet) -> tuple[FreezeRule, ...]:
    """Return matching freeze rules (empty means the write set is allowed)."""
    return tuple(violation.rule for violation in freeze_violation_details(write_set, freeze))
