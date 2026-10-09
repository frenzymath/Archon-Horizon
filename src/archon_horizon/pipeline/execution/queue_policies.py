"""Category budgets share physical capacity; continuation never resets a budget."""

from sqlalchemy import func, select

from ..errors import DomainError
from ..models import QueuePolicy
from ..persistence.schema import tables
from ..providers.usage_accounting import trusted_amount, uncertain_usage


def category(item):
    return item.get("category") or ("maintenance" if item["role"] == "maintainer" else "work")


def policy(run, name):
    if run.get("orchestration") != "objective":
        return None
    raw = run["queue_policies"].get(name)
    if raw is None:
        raise DomainError("unknown_category", "Queue category is not registered for this objective", 422, category=name)
    return QueuePolicy.model_validate(raw)


def validate_enqueue(conn, run, data):
    name = data.category or ("maintenance" if data.role == "maintainer" else "work")
    settings = policy(run, name)
    if settings is None:
        return name, data.model_options.model_dump(mode="json"), data.harness_id
    if settings.role != data.role:
        raise DomainError("category_role_mismatch", "Queue category cannot change permission role", 422)
    assignment = tables["assignment"]
    queued = conn.execute(select(func.count()).select_from(assignment).where(
        assignment.c.run_id == run["id"], assignment.c.category == name,
        assignment.c.status == "pending")).scalar_one()
    if queued >= settings.max_pending:
        raise DomainError("queue_full", "Category queue is full; preserve demand and retry after capacity changes", 409,
                          category=name, max_pending=settings.max_pending)
    options = settings.model_options.model_dump(mode="json") | data.model_options.model_dump(mode="json")
    return name, options, data.harness_id or settings.harness_id


def blocker(conn, scheduler, item, run, *, host_id=None, harness_id=None, harness_slots=None):
    """Return a human-readable admission reason without changing queue ownership."""
    if item.get("pause_reason"):
        return "Session paused: " + item["pause_reason"]
    name = category(item)
    settings = policy(run, name)
    if settings is None:
        return None
    assignment, execution = tables["assignment"], tables["execution"]
    busy = conn.execute(select(func.coalesce(func.sum(1 + func.coalesce(execution.c.native_capacity, 0)), 0)).select_from(execution.join(assignment)).where(
        assignment.c.run_id == run["id"], assignment.c.category == name,
        scheduler.execution_busy_condition())).scalar_one()
    # Explicitly bounded native tools reserve their maximum before parent
    # launch. Uncapped native children do not consume primary category slots.
    if busy >= settings.slots:
        return f"{name} category slots occupied ({busy}/{settings.slots})"
    admitted = conn.execute(select(func.count()).select_from(assignment).where(
        assignment.c.run_id == run["id"], assignment.c.category == name,
        assignment.c.started_at.is_not(None))).scalar_one()
    if not item["started_at"] and settings.max_sessions is not None and admitted >= settings.max_sessions:
        return f"{name} category session budget reached"
    if settings.token_budget is not None:
        usage = tables["usage_record"]
        amount, unknown = conn.execute(select(func.coalesce(func.sum(
            func.coalesce(trusted_amount(usage, "input_tokens"), 0) + func.coalesce(trusted_amount(usage, "output_tokens"), 0)), 0),
            func.coalesce(func.bool_or(uncertain_usage(usage)), False)).select_from(
                usage.join(execution).join(assignment)).where(assignment.c.run_id == run["id"],
                    assignment.c.category == name)).one()
        if unknown:
            return f"{name} category usage needs reconciliation"
        if amount >= settings.token_budget:
            return f"{name} category token budget reached"
    if host_id is not None and harness_slots and settings.role == "worker":
        reserve = max((p.get("reserved_slots", 0) for p in run["queue_policies"].values()
                       if p["role"] == "maintainer"), default=0)
        # A one-slot host still makes progress by alternating work/maintenance;
        # reserving its only slot forever would deadlock the project.
        allowed = max(1, harness_slots - reserve)
        workers = conn.execute(select(func.coalesce(func.sum(1 + func.coalesce(execution.c.native_capacity, 0)), 0)).select_from(execution.join(assignment)).where(
            execution.c.host_id == host_id, assignment.c.role == "worker",
            execution.c.harness_id == harness_id,
            scheduler.execution_busy_condition())).scalar_one()
        if workers >= allowed:
            return "Host capacity reserved for maintenance"
    return None


def native_capacity(conn, scheduler, item, run, slot):
    """Leave delegation native by default; reserve only an explicit child cap.

    None imposes no Horizon cap, including on planners and one-slot hosts. For
    an opted-in cap, reserve its maximum against category, host and provider
    limits before launch. Such reservations survive uncertain physical stops.
    """
    if slot["max_parallel_subagents"] is None:
        return None
    settings = policy(run, category(item))
    if not settings:
        return slot["max_parallel_subagents"]
    if "planner" in item["functions"]:
        return 0  # Short planning passes leave slots for their queued workers.
    assignment, execution = tables["assignment"], tables["execution"]
    weight = func.coalesce(func.sum(1 + func.coalesce(execution.c.native_capacity, 0)), 0)
    used = conn.execute(select(weight).select_from(execution.join(assignment)).where(
        assignment.c.run_id == run["id"], assignment.c.category == category(item),
        scheduler.execution_busy_condition())).scalar_one()
    host_used = conn.execute(select(weight).select_from(execution).where(
        execution.c.host_id == slot["host_id"], execution.c.harness_id == slot["harness_id"],
        scheduler.execution_busy_condition())).scalar_one()
    reserve = max((p.get("reserved_slots", 0) for p in run["queue_policies"].values()
                   if p["role"] == "maintainer"), default=0) if item["role"] == "worker" else 0
    count = min(slot["max_parallel_subagents"], settings.slots - used - 1,
                max(1, slot["execution_slots"] - reserve) - host_used - 1)
    claims = tables["resource_claim"]
    for limit in slot["limits"]:
        claimed = conn.execute(select(func.coalesce(func.sum(claims.c.units), 0)).where(
            claims.c.resource_limit_id == limit["id"], claims.c.released_at.is_(None))).scalar_one()
        maximum = 1 if limit["failure_count"] else limit["max_concurrent"]
        if maximum is not None:
            count = min(count, maximum - claimed - 1)
    return max(0, count)
