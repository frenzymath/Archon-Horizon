"""Scoped planning observations and a read-only global control-plane health view.

The health view is deliberately derived from physical host capacity and durable
assignment rows.  It is an observation, not a reservation: admission still
occurs in the scheduler transaction.  Keeping the same view in every agent
context gives planners enough information to choose a useful child mission
without allowing a provider session to invent capacity.
"""

from datetime import timezone

from sqlalchemy import case, func, or_, select

from .admission import reason, run_usage
from ..persistence.schema import tables
from ..missions.service import LIVE_EXECUTIONS


def _profile(row):
    """Classify an assignment by its explicit profile function.

    Orchestrators retain the maintainer database role for compatibility with
    older records, so the function marker must take precedence here.  Keeping
    this classification in one place prevents supervisors from consuming the
    maintainer budget in health and admission observations.
    """
    functions = row.get("functions") or ()
    if "orchestrator" in functions:
        return "orchestrator"
    if "planner" in functions:
        return "planner"
    if row.get("role") == "maintainer":
        return "maintainer"
    return "worker"


def usable_slots(state):
    """Conservative capacity preview without reserving overlapping shared limits."""
    remaining = {resource["id"]: resource["remaining"] for resource in state["shared_resources"]}
    free = 0
    for pool in sorted(state["healthy_capacity_pools"], key=lambda pool: len(pool["shared_limit_ids"])):
        if not pool["shared_resources_available"]:
            continue
        count = min([pool["free_slots"], *[remaining.get(identifier, 0) for identifier in pool["shared_limit_ids"]]])
        free += count
        for identifier in pool["shared_limit_ids"]:
            remaining[identifier] -= count
    return free


def global_health(conn, service):
    """Return a bounded, read-only view of shared queue and slot health.

    Assignment rows are retained for audit, so this intentionally distinguishes
    *pending* work from duplicate recurring rows.  The scheduler uses the
    admission fields as a conservative gate; agents use the rest to decide
    whether to delegate, repair, or wait for evidence.
    """
    from .scheduler import Scheduler

    host, assignment, execution, run = (tables[name] for name in
        ("host", "assignment", "execution", "run"))
    scheduler = Scheduler(service)
    now = conn.execute(select(func.now())).scalar_one()
    # Include every enabled host, rather than only the current run's hosts.
    # Physical provider and workspace slots are shared across runs.
    host_ids = list(conn.execute(select(host.c.id).where(host.c.mode == "enabled")).scalars())
    capacity = scheduler.capacity(conn, host_ids)
    total_slots = sum(max(0, int(row["execution_slots"])) for row in capacity)
    occupied = 0
    if host_ids:
        occupied = conn.execute(select(func.coalesce(func.sum(1 + func.coalesce(execution.c.native_capacity, 0)), 0)).select_from(execution).where(
            execution.c.host_id.in_(host_ids), scheduler.execution_busy_condition())).scalar_one()
    free_slots = max(0, total_slots - occupied)

    pending_rows = list(conn.execute(select(
        assignment.c.run_id, assignment.c.role, assignment.c.functions,
        assignment.c.automation_id, func.count().label("count"),
        func.min(assignment.c.created_at).label("oldest")).select_from(
            assignment.join(run, assignment.c.run_id == run.c.id)).where(
            run.c.status.in_(("active", "draining")), assignment.c.status == "pending").group_by(
                assignment.c.run_id, assignment.c.role, assignment.c.functions,
                assignment.c.automation_id)).mappings())
    pending_by_role = {"worker": 0, "planner": 0, "maintainer": 0, "orchestrator": 0}
    pending_by_run = {}
    pending_oldest = None
    for row in pending_rows:
        profile = _profile(row)
        pending_by_role[profile] = pending_by_role.get(profile, 0) + int(row["count"])
        run_counts = pending_by_run.setdefault(str(row["run_id"]), {})
        run_counts[profile] = run_counts.get(profile, 0) + int(row["count"])
        if row["oldest"] and (pending_oldest is None or row["oldest"] < pending_oldest):
            pending_oldest = row["oldest"]

    active_rows = conn.execute(select(
        assignment.c.role, assignment.c.functions, func.count().label("count")).select_from(
            assignment.join(run, assignment.c.run_id == run.c.id)).where(
            run.c.status.in_(("active", "draining")),
            assignment.c.status.in_(("pending", "running", "stopping"))).group_by(
                assignment.c.role, assignment.c.functions)).mappings()
    active_by_role = {"worker": 0, "planner": 0, "maintainer": 0, "orchestrator": 0}
    for row in active_rows:
        active_by_role[_profile(row)] = active_by_role.get(_profile(row), 0) + int(row["count"])

    live_rows = conn.execute(select(
        assignment.c.role, assignment.c.functions, assignment.c.run_id,
        func.count().label("count")).select_from(
            execution.join(assignment, execution.c.assignment_id == assignment.c.id)).where(
                execution.c.status.in_(LIVE_EXECUTIONS)).group_by(
                    assignment.c.role, assignment.c.functions, assignment.c.run_id)).mappings()
    live_by_role = {"worker": 0, "planner": 0, "maintainer": 0, "orchestrator": 0}
    live_by_run = {}
    for row in live_rows:
        profile = _profile(row)
        live_by_role[profile] = live_by_role.get(profile, 0) + int(row["count"])
        run_counts = live_by_run.setdefault(str(row["run_id"]), {})
        run_counts[profile] = run_counts.get(profile, 0) + int(row["count"])

    # An objective planner owns its current pass and one prepared successor.
    # Legacy recurrences retain their single outstanding-owner contract.
    duplicate_rows = list(conn.execute(select(
        assignment.c.automation_id, func.count().label("count")).join(run).where(
            assignment.c.automation_id.is_not(None),
            run.c.status.in_(("active", "draining")),
            assignment.c.status.in_(("pending", "running", "stopping"))).group_by(
                assignment.c.automation_id, run.c.orchestration).having(
                    func.count() > case((run.c.orchestration == "objective", 2), else_=1))).mappings())

    # A single-slot installation cannot run a worker and maintainer at once,
    # but it can still run a bounded maintainer pass serially.  Keep one role
    # token whenever any healthy slot exists; the admission layer enforces the
    # physical overlap constraint when the execution actually starts.
    maintainer_budget = 0 if total_slots == 0 else max(1, total_slots // 2)
    pending_maintainers = pending_by_role.get("maintainer", 0)
    live_maintainers = live_by_role.get("maintainer", 0)
    maintainer_headroom = max(0, maintainer_budget - live_maintainers)
    backlog_target = max(8, total_slots * 4)
    pending_total = sum(pending_by_role.values())
    if pending_total and free_slots == 0:
        status = "blocked"
        reasons = ["No physical execution slot is currently free"]
    elif duplicate_rows or pending_total > max(8, total_slots * 8):
        status = "congested"
        reasons = []
        if duplicate_rows:
            reasons.append("Recurring automations have duplicate outstanding assignments")
        if pending_total > max(8, total_slots * 8):
            reasons.append("Pending queue exceeds the bounded global backlog target")
    else:
        status = "healthy"
        reasons = []
    if pending_oldest is not None:
        # PostgreSQL returns an aware timestamp.  The fallback keeps this view
        # usable with lightweight test doubles that provide naive datetimes.
        reference = pending_oldest
        current = now
        if reference.tzinfo is None and current.tzinfo is not None:
            reference = reference.replace(tzinfo=timezone.utc)
        if reference.tzinfo is not None and current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        oldest_age = max(0.0, (current - reference).total_seconds())
    else:
        oldest_age = None
    return {
        "status": status,
        "reasons": reasons,
        "capacity": {
            "total_slots": total_slots,
            "occupied_slots": occupied,
            "free_slots": free_slots,
            "healthy_pools": len(capacity),
        },
        "pending": {
            "total": pending_total,
            "by_role": pending_by_role,
            "by_run": pending_by_run,
            "oldest_age_seconds": oldest_age,
        },
        "active": {"by_role": active_by_role, "live_by_role": live_by_role,
                   "live_by_run": live_by_run},
        "maintainer_admission": {
            "budget": maintainer_budget,
            "queued": pending_maintainers,
            "live": live_maintainers,
            "headroom": maintainer_headroom,
            # Pending rows are obligations, not slot reservations.  They are
            # bounded by backlog_target; only live maintainers consume the
            # role budget and physical execution slots.
            "allowed": bool(maintainer_headroom and pending_maintainers < backlog_target),
            "one_outstanding_per_automation": True,
        },
        "duplicate_automations": len(duplicate_rows),
        "queue_policy": {
            "max_outstanding_per_automation": 1,
            "objective_planner_max_outstanding": 2,
            "backlog_target": backlog_target,
        },
    }


def planning_needed(conn, service, candidate, run_id, now, visited, observations):
    from ..missions.conditions import Evaluation, Truth
    from ..persistence.records import get
    from .refill import _latest_frontier_result
    from .scheduler import Scheduler

    run = get(conn, "run", run_id)
    key = ("planning_summary", run["id"])
    if key not in observations:
        observations[key] = summary(conn, service, run)
    state = observations[key]
    if state["admission"]["new_assignment_blocker"]:
        return Evaluation(Truth.FALSE, state["admission"]["new_assignment_blocker"])
    free = usable_slots(state)
    if not free:
        return Evaluation(Truth.FALSE, "Productive owners already occupy usable capacity")
    table = tables["assignment"]
    rows = list(conn.execute(select(table).where(table.c.run_id == run["id"],
        table.c.id != candidate["id"], table.c.status == "pending", ~table.c.functions.contains(["planner"]))).mappings())
    scheduler = Scheduler(service)
    admission = scheduler.admission_observations(conn, rows)
    hosts = list({pool["host_id"] for pool in state["healthy_capacity_pools"]})
    slots = scheduler.capacity(conn, hosts)
    remaining = {resource["id"]: resource["remaining"] for resource in state["shared_resources"]}
    pool_remaining = {(pool["host_id"], pool["harness_id"]): pool["free_slots"]
        for pool in state["healthy_capacity_pools"]}
    ready = 0
    # Place retained/constrained owners first. This is a read-only conservative
    # preview, not a reservation or an alternative scheduler.
    for row in sorted(rows, key=lambda row: (row["id"] not in admission.get("admission_threads", {}), not bool(row["harness_id"]))):
        if not service.readiness(conn, dict(row), now, visited=visited, observations=observations).ready:
            continue
        for slot in sorted(slots, key=lambda slot: len(slot["limits"])):
            pool = (slot["host_id"], slot["harness_id"])
            limits = [limit["id"] for limit in slot["limits"]]
            if pool_remaining.get(pool, 0) < 1 or any(remaining.get(identifier, 0) < 1 for identifier in limits):
                continue
            admission[("admission_capacity", run["id"])] = [slot]
            if scheduler.admission_blocker(conn, row, observations=admission) is not None:
                continue
            pool_remaining[pool] -= 1
            for identifier in limits:
                remaining[identifier] -= 1
            ready += 1
            break
        if ready >= free:
            break
    if ready >= free:
        return Evaluation(Truth.FALSE, f"{ready} ready owners can fill {free} available slots")
    if candidate.get("automation_id"):
        from .coordination_memory import awaiting_evidence
        obligation = tables["obligation"]
        new_decision = conn.execute(select(obligation.c.id).where(
            obligation.c.assignment_id == candidate["id"], obligation.c.status == "open",
            obligation.c.kind.in_(("decision", "blocker"))).limit(1)).first()
        if not new_decision and awaiting_evidence(conn, run["id"], candidate_id=candidate["id"],
                now=now, timeout_seconds=service.config.coordination_stall_seconds):
            return Evaluation(Truth.FALSE, "Coordination decision recorded; waiting for follow-up evidence")
        rule = get(conn, "automation", candidate["automation_id"])
        if rule["no_progress_count"]:
            frontier = _latest_frontier_result(conn, run["id"])
            finished = conn.execute(select(table.c.id).where(table.c.run_id == run["id"],
                table.c.automation_id.is_(None), ~table.c.functions.contains(["planner"]),
                table.c.finished_at > rule["updated_at"]).limit(1)).first()
            changed = bool(frontier and frontier > rule["updated_at"]) or bool(finished)
            if not changed:
                return Evaluation(Truth.FALSE, "Waiting for changed work after an unproductive planning pass")
    return Evaluation(Truth.TRUE, f"{free} usable slots and only {ready} ready owners; review the work frontier")


def summary(conn, service, run):
    from .scheduler import Scheduler
    from ..missions.service import LIVE_EXECUTIONS

    now = conn.execute(select(func.now())).scalar_one()
    usage = run_usage(conn, run)
    maximum = run["max_assignments"]
    assignment, run_host, execution, claim, item = (tables[name] for name in
        ("assignment", "run_host", "execution", "resource_claim", "forge_item"))
    function = case((assignment.c.functions.contains(["orchestrator"]), "orchestrator"),
                    (assignment.c.functions.contains(["planner"]), "planner"),
                    (assignment.c.role == "maintainer", "maintainer"), else_="worker")
    counts = [dict(row) for row in conn.execute(select(function.label("role"),
        assignment.c.status, func.count().label("count")).where(assignment.c.run_id == run["id"])
        .group_by(function, assignment.c.status).order_by(function, assignment.c.status)).mappings()]
    hosts = list(conn.execute(select(run_host.c.host_id).where(run_host.c.run_id == run["id"],
        run_host.c.enabled.is_(True))).scalars())
    slots = Scheduler(service).capacity(conn, hosts)
    scheduler = Scheduler(service)
    occupied = {(row.host_id, row.harness_id): row.occupied for row in conn.execute(select(
        execution.c.host_id, execution.c.harness_id,
        func.sum(1 + func.coalesce(execution.c.native_capacity, 0)).label("occupied")).where(
        execution.c.host_id.in_(hosts), scheduler.execution_busy_condition())
        .group_by(execution.c.host_id, execution.c.harness_id))}
    limits = {limit["id"]: limit for slot in slots for limit in slot["limits"]}
    used = dict(conn.execute(select(claim.c.resource_limit_id, func.sum(claim.c.units)).where(
        claim.c.resource_limit_id.in_(limits), claim.c.released_at.is_(None))
        .group_by(claim.c.resource_limit_id)).all())
    resources = []
    for identifier, limit in limits.items():
        maximum = 1 if limit["failure_count"] else limit["max_concurrent"]
        resources.append({"id": identifier, "kind": limit["kind"],
            "remaining": None if maximum is None else max(0, maximum - used.get(identifier, 0))})
    remaining = {row["id"]: row["remaining"] for row in resources}
    pools = [{"host_id": slot["host_id"], "harness_id": slot["harness_id"],
        "slots": slot["execution_slots"],
        "occupied": occupied.get((slot["host_id"], slot["harness_id"]), 0),
        "free_slots": max(0, slot["execution_slots"] - occupied.get((slot["host_id"], slot["harness_id"]), 0)),
        "shared_limit_ids": [limit["id"] for limit in slot["limits"]],
        "shared_resources_available": all(remaining[limit["id"]] is None or remaining[limit["id"]] > 0
                                          for limit in slot["limits"]),
    } for slot in slots]
    prs = dict(conn.execute(select(item.c.status, func.count()).where(
        item.c.origin_run_id == run["id"], item.c.kind == "pull_request")
        .group_by(item.c.status)).all())
    global_state = global_health(conn, service)
    # Agents may observe shared pressure, not enumerate other projects' runs.
    global_state["pending"].pop("by_run", None)
    global_state["active"].pop("live_by_run", None)
    return {"admission": {**usage,
        "limit": maximum,
        "remaining_new_assignments": None if maximum is None else max(0, maximum - usage["admitted_assignments"]),
        "new_assignment_blocker": reason(run, usage, now),
        "supervision_blocker": reason(run, usage, now, supervisory=True),
        "retained_assignment_blocker": reason(run, usage, now, already_started=True)},
        "assignments": counts, "pull_requests": prs,
        "global": global_state,
        "healthy_capacity_pools": pools, "shared_resources": resources,
        "capacity_note": "Free slots are physical observations, not admission reservations. Shared limits overlap across pools. "
            "The scheduler also checks run state, conditions, storage, workspace availability and retained host affinity. "
            "Assignment numbers are identifiers, not admission usage; one retained worker can deliver multiple PRs. "
            "Legacy recurrences admit one outstanding assignment; objective planners retain one current pass and one successor. "
            "Parallel work is delegated through explicit child missions."}
