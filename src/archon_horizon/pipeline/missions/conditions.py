"""Bounded three-valued conditions; missing observations cannot admit work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable, Mapping


class Truth(Enum):
    FALSE = 0
    TRUE = 1
    UNKNOWN = 2


@dataclass(frozen=True)
class Evaluation:
    truth: Truth
    reason: str
    next_check_at: datetime | None = None

    @property
    def ready(self) -> bool:
        return self.truth is Truth.TRUE


def evaluate(condition: Mapping | None, now: datetime,
             observe: Callable[[Mapping], Evaluation], *, events_only: bool = False) -> Evaluation:
    """Evaluate a previously validated Condition using a scoped observation callback."""
    if condition is None:
        return Evaluation(Truth.TRUE, "Ready")
    if condition.get("version") != 1:
        return Evaluation(Truth.UNKNOWN, "Unsupported condition version")
    remaining = [128]

    def visit(expr: Mapping, depth: int = 0) -> Evaluation:
        remaining[0] -= 1
        if depth > 8 or remaining[0] < 0:
            return Evaluation(Truth.UNKNOWN, "Condition exceeds its evaluation bound")
        op = expr.get("op")
        if events_only and op in ("after", "queue_below", "planning_needed"):
            return Evaluation(Truth.UNKNOWN, "Scheduling threshold is not an external event")
        if op in ("all", "any"):
            args = expr.get("args", [])
            if not args:
                return Evaluation(Truth.UNKNOWN, "Empty condition")
            children = [visit(child, depth + 1) for child in args]
            decisive = Truth.FALSE if op == "all" else Truth.TRUE
            selected = next((child for child in children if child.truth is decisive), None)
            if selected:
                return selected
            unknown = next((child for child in children if child.truth is Truth.UNKNOWN), None)
            dates = [child.next_check_at for child in children if child.next_check_at]
            return Evaluation(Truth.UNKNOWN if unknown else (
                Truth.TRUE if op == "all" else Truth.FALSE
            ), unknown.reason if unknown else "; ".join(child.reason for child in children),
                min(dates) if dates else None)
        if op == "not":
            result = visit(expr["arg"], depth + 1)
            truth = {Truth.TRUE: Truth.FALSE, Truth.FALSE: Truth.TRUE,
                     Truth.UNKNOWN: Truth.UNKNOWN}[result.truth]
            return Evaluation(truth, "Not: " + result.reason, result.next_check_at)
        if op == "after":
            at = expr["at"]
            if isinstance(at, str):
                at = datetime.fromisoformat(at.replace("Z", "+00:00"))
            if at.tzinfo is None:
                return Evaluation(Truth.UNKNOWN, "Condition timestamp has no timezone")
            return Evaluation(Truth.TRUE if now >= at else Truth.FALSE,
                              "Ready" if now >= at else f"Waiting until {at.isoformat()}",
                              at if now < at else None)
        return observe(expr)

    return visit(condition["expression"])


def referenced_objects(condition: Mapping | None) -> list[dict]:
    """Return concrete edges for authorization and dependency-cycle validation."""
    if not condition:
        return []
    refs: list[dict] = []

    def visit(expr):
        op = expr["op"]
        if op in ("all", "any"):
            for child in expr["args"]:
                visit(child)
        elif op == "not":
            visit(expr["arg"])
        elif op in ("status_in", "revision_after"):
            refs.append(expr["target"])
        elif op == "discussion_changed":
            refs.append({"kind": "discussion", "id": expr["discussion_id"]})
        elif op == "obligation_accounted":
            refs.append({"kind": "obligation", "id": expr["obligation_id"]})
        elif op == "publication_verified":
            refs.append({"kind": "publication", "id": expr["publication_id"]})
        elif op in ("queue_below", "planning_needed"):
            refs.append({"kind": "run", "id": expr["run_id"]})
        elif op in ("forge_open_count", "forge_actionable_count"):
            refs.append({"kind": "project", "id": expr["project_id"]})
            if expr.get("origin_run_id"):
                refs.append({"kind": "run", "id": expr["origin_run_id"]})
            refs.extend({"kind": "repository", "id": value} for value in expr["repository_ids"])
    visit(condition["expression"])
    return refs


def reject_cycle(edges: Mapping[str, set[str]]) -> None:
    """Validate the whole affected dependency component without recursive Python calls."""
    from ..errors import DomainError

    done: set[str] = set()
    active: set[str] = set()
    for root in edges:
        stack = [(root, False)]
        while stack:
            node, leaving = stack.pop()
            if leaving:
                active.discard(node)
                done.add(node)
            elif node in active:
                raise DomainError("dependency_cycle", "Dependency would create a cycle")
            elif node not in done:
                active.add(node)
                stack.append((node, True))
                stack.extend((child, False) for child in edges.get(node, ()))
