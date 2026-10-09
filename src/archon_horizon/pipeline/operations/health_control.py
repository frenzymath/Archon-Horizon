"""Durable health evidence and compare-and-swap orchestration plans.

These helpers deliberately do not execute plans.  The orchestrator records a
bounded observation and a typed proposal; the scheduler or an authorized
executor applies the proposal only while its expected run state still matches.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, StrictInt, model_validator
from sqlalchemy import func, select

from ..errors import DomainError
from ..models import Contract
from ..persistence.records import change, create, get, project_of, transaction_lock
from ..persistence.schema import tables


def supervision_activity(conn, run_id, assignment_ids):
    """Summarize selected owners without treating journal housekeeping as work."""
    from ..execution.intent_reconciliation import JOURNAL_BLOCKER_PREFIX, INTENT_REPAIR_PREFIX

    assignment, execution, request, obligation = (tables[name] for name in
        ("assignment", "execution", "provider_request", "obligation"))
    selected = select(assignment.c.id).where(assignment.c.run_id == run_id,
                                            assignment.c.id.in_(assignment_ids))
    result = {identifier: {"execution_count": 0, "completed_requests": 0,
        "journal_reconciliations": 0, "open_journal_blockers": 0,
        "last_work_accounted_at": None} for identifier in conn.execute(selected).scalars()}
    for row in conn.execute(select(execution.c.assignment_id, func.count().label("execution_count"),
            func.max(execution.c.started_at).label("last_execution_at"))
            .where(execution.c.assignment_id.in_(selected)).group_by(execution.c.assignment_id)).mappings():
        result[row["assignment_id"]].update({key: row[key] for key in ("execution_count", "last_execution_at")})
    for row in conn.execute(select(execution.c.assignment_id, func.count().label("completed_requests"))
            .join(request, request.c.execution_id == execution.c.id)
            .where(execution.c.assignment_id.in_(selected), request.c.reason != "review",
                   request.c.status == "completed").group_by(execution.c.assignment_id)).mappings():
        result[row["assignment_id"]]["completed_requests"] = row["completed_requests"]
    journal = obligation.c.description.startswith(JOURNAL_BLOCKER_PREFIX)
    administrative = journal | obligation.c.description.startswith(INTENT_REPAIR_PREFIX)
    for row in conn.execute(select(obligation.c.assignment_id,
            func.count().filter(journal).label("journal_reconciliations"),
            func.count().filter(journal & (obligation.c.status == "open")).label("open_journal_blockers"),
            func.max(obligation.c.updated_at).filter(
                ~administrative & (obligation.c.status != "open")).label("last_work_accounted_at"))
            .where(obligation.c.assignment_id.in_(selected)).group_by(obligation.c.assignment_id)).mappings():
        result[row["assignment_id"]].update({key: row[key] for key in
            ("journal_reconciliations", "open_journal_blockers", "last_work_accounted_at")})
    return result


class Scope(Contract):
    project_id: UUID | None = None
    run_id: UUID | None = None
    host_id: UUID | None = None
    assignment_id: UUID | None = None

    @model_validator(mode="after")
    def requires_target(self) -> "Scope":
        if not any(value is not None for value in self.model_dump().values()):
            raise ValueError("a health record needs at least one scope identifier")
        return self

    def ids(self) -> dict[str, UUID]:
        values = {name: value for name, value in self.model_dump().items() if value is not None}
        if not values:
            raise ValueError("a health record needs at least one scope identifier")
        return values


class HealthIssueSpec(Contract):
    scope: Scope
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    fingerprint: str = Field(min_length=1, max_length=128)
    severity: Literal["info", "warning", "error", "critical"] = "warning"
    summary: str = Field(min_length=1, max_length=2000)
    details: dict = Field(default_factory=dict)


class HealthSnapshotSpec(Contract):
    scope: Scope
    kind: Literal["system", "run", "host"] = "run"
    frontier_hash: str = Field(min_length=1, max_length=128)
    health_hash: str = Field(min_length=1, max_length=128)
    snapshot_key: str = Field(min_length=1, max_length=200)
    payload: dict
    captured_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def supported_scope(self) -> "HealthSnapshotSpec":
        if self.scope.assignment_id is not None:
            raise ValueError("health snapshots support project, run and host scopes; assignment observations use health issues")
        return self


class ControlPlanSpec(Contract):
    project_id: UUID
    run_id: UUID
    plan_key: str = Field(min_length=1, max_length=200)
    expected_run_revision: StrictInt = Field(gt=0)
    expected_frontier_hash: str | None = Field(default=None, min_length=1, max_length=128)
    actions: dict
    rationale: str = Field(min_length=1, max_length=4000)
    created_by_execution_id: UUID | None = None


def _scope_key(scope: Scope) -> str:
    return ",".join(f"{key}:{value}" for key, value in sorted(scope.ids().items()))


def _dedupe(scope: Scope, kind: str, key: str) -> str:
    raw = f"{kind}|{_scope_key(scope)}|{key}"
    # Keep database keys bounded even when an external connector supplies a
    # long fingerprint or snapshot label.
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_json_object(value: dict, name: str) -> None:
    try:
        json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise DomainError("invalid_health_payload", f"{name} must be JSON serializable", 422) from error


def _validate_scope(conn, scope: Scope) -> None:
    """Keep combined project, run and assignment identifiers consistent."""
    project_id = scope.project_id
    derived_projects = set()
    for kind in ("run", "assignment"):
        identifier = getattr(scope, f"{kind}_id")
        if identifier is None:
            continue
        derived = project_of(conn, kind, identifier)
        derived_projects.add(derived)
        if project_id is not None and derived != project_id:
            raise DomainError("scope_mismatch", "Health scope records must belong to one project", 422)
    if len(derived_projects) > 1:
        raise DomainError("scope_mismatch", "Health scope records must belong to one project", 422)
    if scope.run_id is not None and scope.assignment_id is not None:
        if get(conn, "assignment", scope.assignment_id)["run_id"] != scope.run_id:
            raise DomainError("scope_mismatch", "Health scope assignment must belong to the selected run", 422)


def upsert_health_issue(conn, spec: HealthIssueSpec) -> dict:
    """Record an observation once per scope/code/fingerprint.

    Repeated observations update one revisioned row and increment its
    occurrence count.  A resolved issue is reopened when observed again;
    explicitly suppressed issues remain suppressed until an operator changes
    them.
    """
    _validate_json_object(spec.details, "details")
    transaction_lock(conn)
    _validate_scope(conn, spec.scope)
    key = _dedupe(spec.scope, "issue", f"{spec.code}|{spec.fingerprint}")
    table = tables["health_issue"]
    row = conn.execute(select(table).where(table.c.dedupe_key == key).with_for_update()).mappings().first()
    if row:
        status = "open" if row["status"] == "resolved" else row["status"]
        return change(conn, "health_issue", row["id"], expected_revision=row["revision"],
                      severity=spec.severity, summary=spec.summary, details=spec.details,
                      status=status, resolved_at=None if status == "open" else row["resolved_at"],
                      last_seen_at=func.now(), occurrences=row["occurrences"] + 1)
    values = {**spec.scope.ids(), "dedupe_key": key, "code": spec.code,
              "severity": spec.severity, "status": "open", "summary": spec.summary,
              "details": spec.details, "first_seen_at": func.now(), "last_seen_at": func.now(),
              "occurrences": 1}
    return create(conn, "health_issue", **values)


def capture_health_snapshot(conn, spec: HealthSnapshotSpec) -> dict:
    """Persist one immutable snapshot for a scope and snapshot key."""
    _validate_json_object(spec.payload, "payload")
    transaction_lock(conn)
    _validate_scope(conn, spec.scope)
    key = _dedupe(spec.scope, "snapshot", spec.snapshot_key)
    table = tables["health_snapshot"]
    existing = conn.execute(select(table).where(table.c.dedupe_key == key)).mappings().first()
    if existing:
        return dict(existing)
    captured_at = spec.captured_at or datetime.now(timezone.utc)
    values = {**spec.scope.ids(), "kind": spec.kind, "dedupe_key": key,
              "frontier_hash": spec.frontier_hash, "health_hash": spec.health_hash,
              "payload": spec.payload, "captured_at": captured_at}
    return create(conn, "health_snapshot", **values)


def propose_control_plan(conn, spec: ControlPlanSpec) -> dict:
    """Create a deduplicated proposal against the current run revision."""
    _validate_json_object(spec.actions, "actions")
    transaction_lock(conn)
    run = get(conn, "run", spec.run_id)
    if project_of(conn, "run", spec.run_id) != spec.project_id:
        raise DomainError("scope_mismatch", "Control plan project does not contain the run", 422)
    key = _dedupe(Scope(project_id=spec.project_id, run_id=spec.run_id), "plan", spec.plan_key)
    table = tables["control_plan"]
    existing = conn.execute(select(table).where(table.c.dedupe_key == key)).mappings().first()
    if existing:
        return dict(existing)
    if run["revision"] != spec.expected_run_revision:
        raise DomainError("control_plan_stale", "The run changed before this plan was proposed", 409,
                          current_revision=run["revision"])
    return create(conn, "control_plan", project_id=spec.project_id, run_id=spec.run_id,
                  created_by_execution_id=spec.created_by_execution_id, dedupe_key=key,
                  status="proposed", expected_run_revision=spec.expected_run_revision,
                  expected_frontier_hash=spec.expected_frontier_hash, actions=spec.actions,
                  rationale=spec.rationale)


def accept_control_plan(conn, plan_id: UUID, *, expected_revision: int, note: str | None = None) -> dict:
    transaction_lock(conn)
    plan = get(conn, "control_plan", plan_id, lock=True)
    if plan["status"] != "proposed":
        raise DomainError("control_plan_not_proposed", "Only proposed control plans can be accepted", 409,
                          plan_status=plan["status"])
    return change(conn, "control_plan", plan_id, expected_revision=expected_revision,
                  status="accepted", decision_note=note)


def apply_control_plan(conn, plan_id: UUID, *, expected_revision: int) -> dict:
    """Apply the plan marker only if both plan and target run are unchanged.

    Executing actions remains the caller's responsibility.  It should perform
    its own domain mutations in the same transaction after this guard, or
    reject the plan if those mutations cannot be completed.
    """
    transaction_lock(conn)
    plan = get(conn, "control_plan", plan_id, lock=True)
    if plan["status"] not in {"proposed", "accepted"}:
        raise DomainError("control_plan_not_applicable", "Control plan is no longer applicable", 409,
                          plan_status=plan["status"])
    run = get(conn, "run", plan["run_id"], lock=True)
    if run["revision"] != plan["expected_run_revision"]:
        raise DomainError("control_plan_stale", "The run changed since this plan was proposed", 409,
                          current_revision=run["revision"], expected_revision=plan["expected_run_revision"])
    if plan["expected_frontier_hash"] is not None:
        coordination = conn.execute(select(tables["run_coordination"]).where(
            tables["run_coordination"].c.run_id == run["id"])).mappings().first()
        if coordination is None or coordination["frontier_hash"] != plan["expected_frontier_hash"]:
            raise DomainError("control_plan_stale", "The run frontier changed since this plan was proposed", 409)
    return change(conn, "control_plan", plan_id, expected_revision=expected_revision,
                  status="applied", applied_at=func.now())


__all__ = [
    "Scope", "HealthIssueSpec", "HealthSnapshotSpec", "ControlPlanSpec",
    "upsert_health_issue", "capture_health_snapshot", "propose_control_plan",
    "accept_control_plan", "apply_control_plan",
]
