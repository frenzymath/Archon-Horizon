"""Readable queue conditions, backed by the scheduler's admission evaluation."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select

from .conditions import referenced_objects
from .schema import tables
from .scheduler import Scheduler


def condition_names(conn, rows):
    references = defaultdict(set)
    for row in rows:
        for ref in referenced_objects(row["start_condition"]):
            references[ref["kind"]].add(ref["id"])
    names = {}
    for kind, identifiers in references.items():
        table = tables[kind]
        columns = [table.c.id, *[table.c[key] for key in ("number", "title", "remote_path") if key in table.c]]
        for row in conn.execute(select(*columns).where(table.c.id.in_(identifiers))).mappings():
            label = {"assignment": "Session", "obligation": "Goal item"}.get(kind, kind.capitalize())
            suffix = row.get("title") or row.get("remote_path") or (f"#{row['number']}" if row.get("number") else "")
            names[(kind, str(row["id"]))] = f"{label} {suffix}".strip()
    return names


def describe(condition, names):
    if condition is None:
        return "No start condition"
    if condition.get("version") != 1:
        return "Unsupported condition version"

    def name(kind, identifier):
        return names.get((kind, str(identifier)), {"assignment": "Session", "obligation": "Goal item"}.get(kind, kind.capitalize()))

    def visit(expr):
        op = expr["op"]
        if op in ("all", "any"):
            joiner = "; and " if op == "all" else "; or "
            return "(" + joiner.join(visit(child) for child in expr["args"]) + ")"
        if op == "not":
            return "Not: " + visit(expr["arg"])
        if op == "after":
            return f"After {expr['at']}"
        if op == "status_in":
            target = expr["target"]
            states = ["queued" if state == "pending" and target["kind"] == "assignment" else state.replace("_", " ") for state in expr["values"]]
            return f"{name(target['kind'], target['id'])} is " + " or ".join(states)
        if op == "revision_after":
            target = expr["target"]
            return f"{name(target['kind'], target['id'])} changes after revision {expr['revision']}"
        if op == "discussion_changed":
            return name("discussion", expr["discussion_id"]) + " receives a new or edited message"
        if op == "queue_below":
            return f"Fewer than {expr['count']} ready worker sessions in {name('run', expr['run_id'])}"
        if op == "planning_needed":
            return f"Usable capacity needs more independent work in {name('run', expr['run_id'])}"
        if op == "obligation_accounted":
            return name("obligation", expr["obligation_id"]) + " is completed or handled"
        if op == "publication_verified":
            return name("publication", expr["publication_id"]) + " is verified"
        if op in ("forge_open_count", "forge_actionable_count"):
            kinds = " or ".join("pull requests" if kind == "pull_request" else "issues" for kind in expr["kinds"])
            scope = ", ".join(name("repository", identifier) for identifier in expr["repository_ids"]) or name("project", expr["project_id"])
            selectors = []
            if expr.get("origin_run_id"):
                selectors.append(name("run", expr["origin_run_id"]))
            if expr.get("review_phase"):
                selectors.append(expr["review_phase"] + " phase")
            if selectors:
                scope += " (" + ", ".join(selectors) + ")"
            labels = f" with {expr['match']} of these labels: {', '.join(expr['labels'])}" if expr["labels"] else ""
            qualifier = "actionable " if op == "forge_actionable_count" else "open "
            return f"At least {expr['at_least']} {qualifier}{kinds} in {scope}{labels}"
        return "Unsupported condition"

    return visit(condition["expression"])


def admissions(conn, rows, service, now):
    pending = [row for row in rows if row["status"] == "pending"]
    names = condition_names(conn, pending)
    scheduler = Scheduler(service)
    observations = scheduler.admission_observations(conn, pending)
    result = {}
    for row in pending:
        state = service.readiness(conn, row, now, observations=observations)
        blocker = scheduler.admission_blocker(conn, row, observations=observations) if state.ready else None
        result[row["id"]] = {
            "summary": describe(row["start_condition"], names),
            "state": "waiting" if blocker else {"TRUE": "ready", "FALSE": "waiting", "UNKNOWN": "unknown"}[state.truth.name],
            "reason": blocker or state.reason.replace("ready assignments", "ready worker sessions").replace("assignment is", "Session is"),
            "checked_at": now, "next_check_at": state.next_check_at,
            "not_before": row["not_before"], "expires_at": row["expires_at"],
        }
    return result
