"""Bounded planning opportunities when useful queue work cannot fill capacity."""

from sqlalchemy import func, or_, select

from .conditions import Evaluation, Truth, evaluate
from .records import change, create, emit, get, next_number, project_of, snapshot
from .schema import tables


def _capacity_wait(condition):
    if not condition or condition.get("version") != 1:
        return False
    pending, capacity = [condition["expression"]], False
    while pending:
        expression = pending.pop()
        op = expression["op"]
        if op in ("all", "any"):
            pending.extend(expression["args"])
        elif op == "not":
            pending.append(expression["arg"])
        elif op in ("planning_needed", "queue_below"):
            capacity = True
        elif op != "after":
            return False
    return capacity


def _latest_frontier_result(conn, run_id):
    assignment, publication, item, event, reference = (tables[name] for name in
        ("assignment", "publication", "forge_item", "event", "object_reference"))
    published = conn.execute(select(func.max(publication.c.verified_at)).join(assignment,
        publication.c.requested_by_assignment_id == assignment.c.id).where(
        assignment.c.run_id == run_id, assignment.c.automation_id.is_(None),
        ~assignment.c.functions.contains(["planner"]), publication.c.status == "verified")).scalar_one()
    # observed_at/updated_at also advance on ordinary Forge polling. Only a
    # recorded status transition is evidence of a newly integrated frontier.
    merged = conn.execute(select(func.max(event.c.created_at)).select_from(event.join(reference,
        event.c.subject_id == reference.c.id).join(item, reference.c.forge_item_id == item.c.id)).where(
        item.c.origin_run_id == run_id, item.c.status == "merged", item.c.kind == "pull_request",
        or_(event.c.payload["changed_fields"].contains(["status"]),
            event.c.payload["current"].astext == "merged"))).scalar_one()
    return max((value for value in (published, merged) if value is not None), default=None)


def recheck(scheduler, conn, actor, now):
    interval = scheduler.config.automation_idle_recheck_seconds
    if not interval:
        return 0
    service = scheduler.service
    run, assignment, automation = (tables[name] for name in ("run", "assignment", "automation"))
    candidates = list(conn.execute(select(assignment).join(run, assignment.c.run_id == run.c.id)
        .join(automation, assignment.c.automation_id == automation.c.id).where(
        run.c.status == "active", assignment.c.status == "pending", assignment.c.role == "worker",
        assignment.c.functions.contains(["planner"]), automation.c.enabled.is_(True))).mappings())
    woke, checked = 0, set()
    for candidate in candidates:
        rid = candidate["run_id"]
        if rid in checked:
            continue
        checked.add(rid)
        from .coordination_memory import awaiting_evidence
        if awaiting_evidence(conn, rid, candidate_id=candidate["id"]):
            continue
        if candidate["not_before"] and candidate["not_before"] > now:
            continue
        # Spare capacity can reconsider a queue heuristic, never an agent's
        # dependency on an owner, publication, review or other external event.
        if not _capacity_wait(candidate["start_condition"]):
            continue
        # Treat capacity observations as unknown: FALSE then proves that no
        # capacity change can satisfy the condition before its explicit time gate.
        timed = evaluate(candidate["start_condition"], now,
                         lambda _: Evaluation(Truth.UNKNOWN, "Capacity condition"))
        if timed.truth is Truth.FALSE:
            continue
        rows = list(conn.execute(select(assignment).where(assignment.c.run_id == rid)).mappings())
        if any(row["status"] in ("running", "stopping") and "planner" in row["functions"] for row in rows):
            continue
        observations = {}
        pending = [dict(row) for row in rows if row["status"] == "pending"]
        evaluations = [(row, service.readiness(conn, row, now, observations=observations)) for row in pending]
        admission = scheduler.admission_observations(conn, pending)
        if any(result.ready and scheduler.admission_blocker(conn, row, observations=admission) is None
               for row, result in evaluations):
            continue
        rule = get(conn, "automation", candidate["automation_id"])
        result_at = _latest_frontier_result(conn, rid)
        progress = any(row["automation_id"] is None and "planner" not in row["functions"]
                       and max(row["created_at"], row["finished_at"] or row["created_at"]) > rule["updated_at"]
                       for row in rows)
        progress = progress or bool(result_at and result_at > rule["updated_at"])
        # One reconsideration is enough without a changed work frontier. A
        # waiting planner must not consume another session every polling period.
        if rule["no_progress_count"] and not progress:
            continue
        last_change = max([candidate["created_at"], rule["updated_at"]]
                          + [row["finished_at"] for row in rows if row["finished_at"] and row["automation_id"] is None]
                          + ([result_at] if result_at else []))
        if (now - last_change).total_seconds() < interval:
            continue
        probe = dict(candidate, start_condition=None)
        if (not service.readiness(conn, probe, now).ready
                or scheduler.admission_blocker(conn, probe, observations=admission) is not None):
            continue
        note = "Idle queue recheck: usable capacity exists and no queued session can currently use it."
        detail = "; ".join(f"A{row['number']}: {result.reason}" for row, result in evaluations)[:4000]
        row = change(conn, "assignment", candidate["id"], start_condition=None, status_note=note)
        create(conn, "obligation", assignment_id=row["id"],
            number=next_number(conn, "obligation", "assignment_id", row["id"]), kind="decision",
            description=note + " Inspect current blockers and the independent work frontier. "
            "Delegate only concrete useful work with available inputs; otherwise record a justified wait. "
            "Do not duplicate owners or bypass review or work dependencies. " + detail)
        updated = change(conn, "automation", rule["id"], no_progress_count=1)
        snapshot(conn, "automation", updated, actor.id)
        snapshot(conn, "assignment", row, actor.id)
        emit(conn, actor.id, project_of(conn, "run", rid), "assignment", row,
             ["start_condition", "status_note"], note=note)
        woke += 1
    return woke
