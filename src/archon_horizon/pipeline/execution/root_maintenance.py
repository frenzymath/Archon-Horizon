"""One evidence-driven maintainer entrypoint, using existing queue and watch state."""

from sqlalchemy import func, select, update

from .coordination_memory import frontier, state
from ..instructions.templates import template
from ..persistence.records import change, create, get, next_number
from ..persistence.schema import tables

NAME = "root-maintainer"
BLOCKED_PREFIX = "Maintenance blocked: "


def rule(conn, run_id):
    automation = tables["automation"]
    row = conn.execute(select(automation).where(
        automation.c.run_id == run_id, automation.c.name == NAME)).mappings().first()
    return dict(row) if row else None


def default_condition(condition):
    expression = (condition or {}).get("expression", {})
    return (expression.get("op") == "any"
            and {child.get("op") for child in expression.get("args", [])}
            == {"planning_needed", "forge_actionable_count"})


def previous_condition(conn, automation):
    assignment, execution, revision = (tables[name] for name in
        ("assignment", "execution", "record_revision"))
    return conn.execute(select(revision.c.content["start_condition"].label("start_condition"))
        .select_from(assignment.join(execution, execution.c.assignment_id == assignment.c.id)
            .join(revision, execution.c.assignment_revision_id == revision.c.id))
        .where(assignment.c.automation_id == automation["id"],
            revision.c.content["start_condition"].astext.is_not(None))
        .order_by(execution.c.created_at.desc(), execution.c.id.desc()).limit(1)).first()


def admission_blocker(conn, assignment, automation):
    if automation["name"] != NAME or assignment["started_at"] is not None:
        return None
    watch = state(conn, assignment["run_id"])
    if not watch:
        return None
    if watch["audit_obligation_id"]:
        audit = get(conn, "obligation", watch["audit_obligation_id"])
        if audit["assignment_id"] == assignment["id"] and audit["status"] == "open":
            return None
    if assignment["start_condition"] and not default_condition(assignment["start_condition"]):
        previous = previous_condition(conn, automation)
        # Honor a newly promised event once. A predicate already consumed by
        # the preceding pass must not become permission for endless recurrence.
        if previous is None or previous.start_condition != assignment["start_condition"]:
            return None
    if frontier(conn, assignment["run_id"]) != watch["frontier_hash"]:
        return None
    return "Waiting for a new worker result, PR head, or review decision; spare capacity is not new work"


def admitted(conn, assignment, now):
    if not assignment["automation_id"]:
        return
    automation = get(conn, "automation", assignment["automation_id"])
    if automation["name"] != NAME:
        return
    watch = state(conn, assignment["run_id"])
    observed = frontier(conn, assignment["run_id"])
    values = {"frontier_hash": observed, "checked_at": now}
    table = tables["run_coordination"]
    if watch:
        if watch["frontier_hash"] != observed:
            values["last_progress_at"] = now
        conn.execute(update(table).where(table.c.run_id == assignment["run_id"]).values(**values))
    else:
        conn.execute(table.insert().values(run_id=assignment["run_id"], last_progress_at=now, **values))
    campaign = get(conn, "run", assignment["run_id"])
    if (campaign["status_note"] or "").startswith(BLOCKED_PREFIX):
        change(conn, "run", campaign["id"], status_note=None)


def recovery_cause(conn, automation, service):
    """Diagnose an ownerless run without treating a declared wait as a failure."""
    assignment = tables["assignment"]
    latest = conn.execute(select(assignment).where(assignment.c.automation_id == automation["id"])
        .order_by(assignment.c.number.desc()).limit(1)).mappings().first()
    if not latest:
        return None
    if latest["status"] in ("failed", "cancelled"):
        return f"Root maintainer A{latest['number']} {latest['status']}: {latest['status_note'] or 'Inspect its preserved work and execution failure'}"
    if latest["status"] != "completed":
        return None
    if automation["start_condition"] and not default_condition(automation["start_condition"]):
        previous = previous_condition(conn, automation)
        if previous is None or previous.start_condition != automation["start_condition"]:
            return None
        now = conn.execute(select(func.now())).scalar_one()
        if not service.condition_readiness(conn, {**latest, "start_condition": automation["start_condition"]}, now).ready:
            return None
    active = conn.execute(select(assignment.c.id).where(assignment.c.run_id == automation["run_id"],
        assignment.c.automation_id.is_distinct_from(automation["id"]),
        assignment.c.status.in_(("pending", "running", "stopping"))).limit(1)).first()
    watch = state(conn, automation["run_id"])
    if not active and watch and frontier(conn, automation["run_id"]) == watch["frontier_hash"]:
        return "The root maintainer finished while the mission remains open, with no follow-up owner or explicit event wait"
    return None


def recovery_available(conn, automation, cause):
    if not cause:
        return True
    observed = frontier(conn, automation["run_id"])
    watch = state(conn, automation["run_id"])
    if not watch or watch["audit_frontier_hash"] != observed:
        return True
    # A completed recovery decision is evidence that the previous owner was
    # accounted for.  Default root maintenance may therefore take one fresh
    # bounded pass on the same frontier.  Event-conditioned waits remain
    # consumed: they must observe a new event instead of being resurrected by
    # a resolved audit record.
    if default_condition(automation["start_condition"]):
        audit = get(conn, "obligation", watch["audit_obligation_id"]) if watch["audit_obligation_id"] else None
        latest = conn.execute(select(tables["assignment"]).where(
            tables["assignment"].c.automation_id == automation["id"])
            .order_by(tables["assignment"].c.number.desc()).limit(1)).mappings().first()
        # A completed audit authorizes one replacement owner. Once that owner
        # has finished, the audit is consumed; otherwise an unchanged frontier
        # would re-admit an identical root pass forever.
        if (audit and latest and audit["assignment_id"] == latest["id"]
                and audit["status"] in ("done", "handled") and audit["resolution"]):
            return True
    note = (BLOCKED_PREFIX + cause + ". Recovery already considered this unchanged evidence. "
            "Inspect the recovery decision and repair the cause; a new substantive result or explicit operator recovery can resume work.")
    campaign = get(conn, "run", automation["run_id"])
    if (campaign["status_note"] != note
            and (not campaign["status_note"] or campaign["status_note"].startswith(BLOCKED_PREFIX))):
        change(conn, "run", campaign["id"], status_note=note)
    return False


def attach_recovery(conn, assignment, cause):
    if not cause:
        return
    observed = frontier(conn, assignment["run_id"])
    obligation = create(conn, "obligation", assignment_id=assignment["id"],
        number=next_number(conn, "obligation", "assignment_id", assignment["id"]), kind="decision",
        description=template("legacy/recovery-obligation", cause=cause))
    now = conn.execute(select(func.now())).scalar_one()
    table = tables["run_coordination"]
    values = {"audit_obligation_id": obligation["id"], "audit_frontier_hash": observed, "checked_at": now}
    if state(conn, assignment["run_id"]):
        conn.execute(update(table).where(table.c.run_id == assignment["run_id"]).values(**values))
    else:
        conn.execute(table.insert().values(run_id=assignment["run_id"], frontier_hash=observed,
            last_progress_at=now, **values))


def inspect_pending(scheduler, conn, automation, assignment):
    """Reuse the waiting root for an observed queue failure, never a second owner."""
    if assignment["started_at"] is not None or not default_condition(assignment["start_condition"]):
        return
    watch = state(conn, assignment["run_id"])
    if not watch or frontier(conn, assignment["run_id"]) != watch["frontier_hash"]:
        return
    if watch["audit_obligation_id"]:
        audit = get(conn, "obligation", watch["audit_obligation_id"])
        if audit["assignment_id"] == assignment["id"] and audit["status"] == "open":
            return
    table = tables["assignment"]
    owners = list(conn.execute(select(table).where(table.c.run_id == assignment["run_id"],
        table.c.id != assignment["id"], table.c.status.in_(("pending", "running", "stopping")))).mappings())
    if not owners or any(owner["status"] != "pending" for owner in owners):
        return
    from ..missions.conditions import Truth
    now = conn.execute(select(func.now())).scalar_one()
    issues = []
    observations = {}
    admission = scheduler.admission_observations(conn, owners)
    for owner in owners:
        readiness = scheduler.service.readiness(conn, dict(owner), now, observations=observations)
        if readiness.ready:
            previous = admission["admission_threads"].get(owner["id"])
            if previous and previous["status"] not in ("creating", "available"):
                issues.append(f"A{owner['number']}: retained context is unavailable")
                continue
            harness_id = (get(conn, "record_revision", previous["harness_revision_id"])["content"]["id"]
                          if previous else owner["harness_id"])
            if not harness_id:
                return
            pool, run_host, harness = (tables[name] for name in ("host_harness", "run_host", "harness"))
            configured = conn.execute(select(pool.c.host_id).join(run_host, run_host.c.host_id == pool.c.host_id)
                .join(harness, harness.c.id == pool.c.harness_id).where(run_host.c.run_id == owner["run_id"],
                    run_host.c.enabled.is_(True), pool.c.harness_id == harness_id,
                    pool.c.enabled.is_(True), harness.c.enabled.is_(True)).limit(1)).first()
            # Occupancy, cooldowns and host recovery are infrastructure waits.
            # Only a missing compatible configuration needs a model decision.
            if configured:
                return
            issues.append(f"A{owner['number']}: no enabled run host provides its requested harness")
        elif readiness.truth is Truth.UNKNOWN:
            issues.append(f"A{owner['number']}: {readiness.reason}")
    if issues:
        cause = "No productive owner can start; " + "; ".join(issues[:8])
        if recovery_available(conn, automation, cause):
            attach_recovery(conn, assignment, cause)
