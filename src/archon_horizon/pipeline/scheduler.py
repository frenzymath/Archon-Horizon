"""Shared-slot admission and a single lifecycle for failures, cancellation and yielding."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
import random
from uuid import UUID, uuid5

from sqlalchemy import and_, case, func, or_, select, update

from . import models
from .auth import Actor, issue_credential, require_host, require_project
from .conditions import Truth
from .errors import DomainError
from .records import change, create, emit, get, json_value, next_number, project_of, same_project, save_blob, snapshot
from .schema import tables
from .service import LIVE_EXECUTIONS, Service

TRANSIENT_CODES = {"connection_error", "rate_limited", "provider_overloaded", "timeout", "host_lost", "lease_expired",
                   "local_operation_timeout", "local_execution_unavailable", "request_deadline"}
CONFIGURATION_CODES = {"authentication_failed", "invalid_configuration", "storage_full", "provider_missing"}


class Scheduler:
    # Keep each scheduler tick bounded when an old run leaves a large queue.
    # Later ticks revisit the remaining page; physical admission is still the
    # final source of truth.
    CLAIM_SCAN_LIMIT = 512

    def __init__(self, service: Service):
        self.service = service
        self.config = service.config
        # Polling cursors are hints, never reservations or admission authority.
        # Losing them on API restart only restarts each bounded scan at the front.
        self._claim_scan_offsets: dict[tuple[UUID, bool], int] = {}

    @staticmethod
    def retained_thread_for_host(conn, assignment_id, host_id):
        """Choose a resumable primary context for the host being admitted."""
        thread, workspace = tables["provider_thread"], tables["workspace"]
        return conn.execute(select(thread).join(workspace, thread.c.workspace_id == workspace.c.id).where(
            thread.c.assignment_id == assignment_id, thread.c.kind == "primary",
            thread.c.status.in_(("creating", "available")), workspace.c.host_id == host_id)
            .order_by(thread.c.updated_at.desc(), thread.c.id.desc())).mappings().first()

    @staticmethod
    def has_native_foreign_thread(conn, assignment_id, host_id):
        """Return whether another host owns a context that cannot be cloned."""
        thread, workspace = tables["provider_thread"], tables["workspace"]
        return conn.execute(select(thread.c.id).join(workspace, thread.c.workspace_id == workspace.c.id).where(
            thread.c.assignment_id == assignment_id, thread.c.kind == "primary",
            thread.c.status.in_(("creating", "available")), workspace.c.host_id != host_id,
            or_(thread.c.status == "available", thread.c.provider_thread_id.is_not(None))).limit(1)).first() is not None

    def retire_orphaned_prelaunch_threads(self, conn, actor, assignment_id, host_id):
        """Release foreign prelaunch records after their execution was fenced.

        A creating thread without a native provider id or unsettled request is
        only an admission reservation.  Leaving it live can violate the
        single-primary constraint while the worker that owns its host is gone.
        """
        thread, workspace, request, execution = (tables[name] for name in
            ("provider_thread", "workspace", "provider_request", "execution"))
        rows = list(conn.execute(select(thread).join(workspace, thread.c.workspace_id == workspace.c.id).where(
            thread.c.assignment_id == assignment_id, thread.c.kind == "primary",
            thread.c.status == "creating", workspace.c.host_id != host_id,
            thread.c.provider_thread_id.is_(None),
            ~select(request.c.id).where(request.c.provider_thread_id == thread.c.id,
                request.c.status.in_(("pending", "submitted", "running", "uncertain"))).exists(),
            ~select(execution.c.id).where(execution.c.assignment_id == assignment_id,
                execution.c.status.in_(LIVE_EXECUTIONS)).exists(),
            select(execution.c.id).where(execution.c.assignment_id == assignment_id,
                execution.c.status.in_(("lost", "failed", "cancelled"))).exists(),
        )).mappings())
        for row in rows:
            retired = change(conn, "provider_thread", row["id"], status="unavailable")
            emit(conn, actor.id, project_of(conn, "assignment", assignment_id), "provider_thread", retired,
                 ["status"], previous="creating",
                 note="Retired a fenced foreign prelaunch context so a healthy local worker can claim the assignment")
        return len(rows)

    def retire_failed_primary_contexts(self, conn, actor, assignment_id):
        """Do not resume a native context whose latest provider turn failed."""
        thread, request, execution, assignment = (tables[name] for name in
            ("provider_thread", "provider_request", "execution", "assignment"))
        item = conn.execute(select(assignment.c.status).where(assignment.c.id == assignment_id)).scalar_one()
        if item != "pending":
            return 0
        rows = list(conn.execute(select(thread).where(thread.c.assignment_id == assignment_id,
            thread.c.kind == "primary", thread.c.status == "available",
            thread.c.provider_thread_id.is_not(None),
            ~select(execution.c.id).where(execution.c.assignment_id == assignment_id,
                execution.c.status.in_(LIVE_EXECUTIONS)).exists())).mappings())
        retired = 0
        for row in rows:
            latest = conn.execute(select(request.c.status, request.c.failure).where(
                request.c.provider_thread_id == row["id"])
                .order_by(request.c.created_at.desc(), request.c.id.desc()).limit(1)).mappings().first()
            if not latest or latest["status"] != "failed":
                continue
            changed = change(conn, "provider_thread", row["id"], status="unavailable")
            emit(conn, actor.id, project_of(conn, "assignment", assignment_id), "provider_thread", changed,
                 ["status"], previous="available",
                 note="Retired a failed native context pending explicit recovery")
            retired += 1
        return retired

    def capacity(self, conn, host_ids: list[UUID]) -> list[dict]:
        hh, host, harness = (tables[name] for name in ("host_harness", "host", "harness"))
        now = conn.execute(select(func.now())).scalar_one()
        rows = [dict(row) for row in conn.execute(select(hh).join(host).join(harness).where(
            hh.c.host_id.in_(host_ids), hh.c.enabled.is_(True), host.c.mode == "enabled",
            or_(host.c.health.is_(None), host.c.health["status"].astext != "storage_pressure"),
            harness.c.enabled.is_(True), host.c.heartbeat_at > now - timedelta(seconds=self.config.host_stale_seconds),
        )).mappings()]
        limits, linkage = tables["resource_limit"], tables["host_harness_limit"]
        for row in rows:
            row["limits"] = [dict(limit) for limit in conn.execute(select(limits).join(linkage).where(
                linkage.c.host_id == row["host_id"], linkage.c.harness_id == row["harness_id"],
            )).mappings()]
        return [row for row in rows if not any(limit["cooldown_until"] and limit["cooldown_until"] > now
                                               for limit in row["limits"])]

    @staticmethod
    def two_usable_slots(capacity: list[dict]) -> bool:
        # Two concrete allocations account for overlapping provider/build limits.
        for index, first in enumerate(capacity):
            for second in capacity[index:]:
                if first is second and first["execution_slots"] < 2:
                    continue
                needed: dict[UUID, int] = {}
                maxima: dict[UUID, int] = {}
                for row in (first, second):
                    for limit in row["limits"]:
                        needed[limit["id"]] = needed.get(limit["id"], 0) + 1
                        maxima[limit["id"]] = limit["max_concurrent"]
                if all(used <= maxima[key] for key, used in needed.items()):
                    return True
        return False

    def run(self, conn, actor: Actor, data: models.RunCreate) -> dict:
        mission = get(conn, "mission", data.mission_id)
        project_id = mission["project_id"]
        require_project(conn, actor, project_id, "maintainer")
        if mission["status"] != "open":
            raise DomainError("mission_closed", "An active run requires an open mission")
        if not self.two_usable_slots(self.capacity(conn, data.host_ids)):
            raise DomainError("insufficient_capacity", "At least two compatible, healthy shared slots are required")
        phase = data.phase.model_dump(mode="json")
        baseline = None
        if phase["kind"] == "preprocessing":
            document = same_project(conn, "document", phase["roadmap_document_id"], project_id)
            if document["kind"] != "roadmap":
                raise DomainError("invalid_phase_input", "Preprocessing requires a roadmap document", 422)
            self.matching_policy(conn, document["source_repository_id"], phase["kind"], required=True)
        elif phase["kind"] == "formalization":
            baseline = UUID(phase["roadmap_snapshot_id"])
            snap = same_project(conn, "roadmap_snapshot", baseline, project_id)
            from .milestones import require_baseline
            require_baseline(conn, snap)
            document = get(conn, "document", snap["roadmap_document_id"])
            self.matching_policy(conn, document["source_repository_id"], phase["kind"], required=True)
        else:
            workspace = same_project(conn, "workspace", phase["source_workspace_id"], project_id)
            repository = same_project(conn, "repository", phase["target_repository_id"], project_id)
            if workspace["status"] != "ready" or repository["purpose"] != "library":
                raise DomainError("invalid_phase_input", "Postprocessing requires a ready workspace and a library target", 422)
            # The daemon verifies this source before reporting its workspace ready.
            if phase["source_commit_oid"] not in (workspace["head_commit_oid"], workspace["base_commit_oid"]):
                raise DomainError("source_unverified", "Register a workspace verified at the requested source commit", 422)
            self.matching_policy(conn, repository["id"], phase["kind"], required=True)
        from .milestones import enabled
        if (phase['kind'] in {'preprocessing', 'formalization'} and enabled(conn, project_id)
                and mission['roadmap_document_id'] != document['id']):
            raise DomainError('objective_mismatch', 'Link the mission to the selected milestone objective', 422)
        row = create(conn, "run", **data.model_dump(exclude={"host_ids", "phase", "retry_policy"}),
                     phase=phase, adopted_roadmap_snapshot_id=baseline,
                     retry_policy=data.retry_policy.model_dump(), started_at=func.now())
        for host_id in set(data.host_ids):
            conn.execute(tables["run_host"].insert().values(run_id=row["id"], host_id=host_id))
        phase_repository_ids = ([phase["target_repository_id"]] if phase["kind"] == "postprocessing"
                                else [str(document["source_repository_id"])])
        automation_specs = self.automation_specs(row, phase, project_id, phase_repository_ids)
        for name, role, functions, objective, condition, enabled in automation_specs:
            automation = create(conn, "automation", run_id=row["id"], name=name, role=role,
                                functions=functions, mission_id=mission["id"], instructions=objective,
                                cooldown_seconds=120 if "orchestrator" in functions else 15 if role == "maintainer" else 120,
                                enabled=enabled, start_condition=condition)
            snapshot(conn, "automation", automation, actor.id)
            if enabled:
                self.replenish(conn, actor, automation)
        emit(conn, actor.id, project_id, "run", row, ["phase", "status"])
        return row

    @staticmethod
    def automation_specs(run, phase, project_id, phase_repository_ids):
        if phase.get("orchestrated", False):
            # The model is a short-lived control episode.  It receives the
            # whole phase contract and current health snapshot, then queues
            # narrow child work itself.  Scheduler safety remains deterministic
            # and the episode is renewed only after its cooldown.
            return (
                ("orchestrator", "maintainer", ["orchestrator"],
                 "Run one bounded control pass: inspect Horizon health and queue consistency, activate only the required planner or maintainer automation, report incidents through operations Zulip, and finish without mathematical or repository work.",
                 None, True),
                ("planner", "worker", ["planner"],
                 "Perform a bounded planning pass over the current frontier and dispatch narrow executable worker missions, then finish.",
                 {"version": 1, "expression": {"op": "planning_needed", "run_id": str(run["id"])}}, False),
                ("maintainer", "maintainer", [],
                "Perform a bounded review/integration pass over actionable Forge work, then finish.",
                {"version": 1, "expression": {"op": "forge_actionable_count", "project_id": str(project_id),
                    "origin_run_id": str(run["id"]), "review_phase": phase["kind"],
                    "repository_ids": phase_repository_ids, "kinds": ["pull_request", "issue"], "labels": [], "match": "any", "at_least": 1}}, False),
            )
        from .root_maintenance import NAME
        return ((NAME, "maintainer", [],
            "Own the run's next bounded maintenance pass: plan concrete unowned work, delegate scoped workers, "
            "review their results and assign repairs, or record phase completion. Preserve existing owners and "
            "use explicit terminal-event waits while they work, including failed and cancelled outcomes. "
            "Retain root and ancestor closure responsibility when delegating child integration. "
            "Use a diagnostic subagent only for an observed anomaly; finish the pass after accounting for its decision.",
            {"version": 1, "expression": {"op": "any", "args": [
                {"op": "planning_needed", "run_id": str(run["id"])},
                {"op": "forge_actionable_count", "project_id": str(project_id),
                 "origin_run_id": str(run["id"]), "review_phase": phase["kind"],
                 "repository_ids": phase_repository_ids, "kinds": ["pull_request", "issue"],
                 "labels": [], "match": "any", "at_least": 1}]}}, True),)

    def matching_policy(self, conn, repository_id, phase, *, required=False):
        policy, link = tables["review_policy"], tables["review_policy_repository"]
        rows = list(conn.execute(select(policy).join(link).where(link.c.repository_id == repository_id,
            policy.c.enabled.is_(True), policy.c.phases.contains([phase]))).mappings())
        if len(rows) > 1:
            raise DomainError("ambiguous_review_policy", "Repository and phase match multiple review policies")
        if required and not rows:
            raise DomainError("missing_review_policy", "Protected repository needs a review policy for this phase")
        return dict(rows[0]) if rows else None

    def replenish(self, conn, actor, automation: dict, *, health: dict | None = None) -> dict | None:
        run = get(conn, "run", automation["run_id"])
        if not automation["enabled"] or run["status"] != "active":
            return None
        table = tables["assignment"]
        # Pending owners reserve their frontier, including deliberate waits.
        # Coordination recovery can repair a bad gate without duplicating it.
        active = conn.execute(select(func.count()).select_from(table).where(
            table.c.automation_id == automation["id"],
            table.c.status.in_(("running", "stopping")))).scalar_one()
        pending = conn.execute(select(table.c.id).where(
            table.c.automation_id == automation["id"], table.c.status == "pending").limit(1)).first()
        from .root_maintenance import NAME, attach_recovery, inspect_pending, recovery_available, recovery_cause
        if pending and not active and automation["name"] == NAME:
            inspect_pending(self, conn, automation, get(conn, "assignment", pending[0]))
        active += bool(pending)
        # Pending or executing occurrences retain their recurrence ownership.
        if active:
            return None
        recovery = None
        if automation["name"] == NAME:
            if get(conn, "mission", automation["mission_id"])["status"] != "open":
                return None
            recovery = recovery_cause(conn, automation, self.service)
            if not recovery_available(conn, automation, recovery):
                return None
        from .review_backlog import recurring_maintenance_blocker
        if recurring_maintenance_blocker(conn, automation, automation):
            return None
        if automation["role"] == "maintainer" and "orchestrator" not in automation["functions"] and not self.maintainer_admission_available(conn, health=health):
            return None
        row = self.service.assignment(conn, actor, models.AssignmentCreate(
            run_id=automation["run_id"], mission_id=automation["mission_id"], role=automation["role"],
            functions=automation["functions"], instructions=automation["instructions"],
            not_before=automation["not_before"], start_condition=None if recovery else automation["start_condition"],
        ), automation_id=automation["id"], internal=True)
        attach_recovery(conn, row, recovery)
        return row

    def maintainer_admission_available(self, conn, *, health: dict | None = None) -> bool:
        """Check the shared maintainer budget before creating a recurrence row."""
        from .coordination import global_health
        health = health or global_health(conn, self.service)
        return bool(health["maintainer_admission"]["allowed"])

    def replenish_maintainers(self, conn, actor) -> int:
        """Replenish enabled review profiles; orchestrators have their own cooldown."""
        from .coordination import global_health
        health = global_health(conn, self.service)
        created = 0
        automation = tables["automation"]
        rows = conn.execute(select(automation).join(tables["run"], automation.c.run_id == tables["run"].c.id).where(
            automation.c.enabled.is_(True), automation.c.role == "maintainer",
            ~automation.c.functions.contains(["orchestrator"]),
            tables["run"].c.status == "active")).mappings()
        for rule in rows:
            rule = dict(rule)
            # The profile's condition still gates physical admission.
            if self.replenish(conn, actor, dict(rule), health=health) is not None:
                created += 1
                health["maintainer_admission"]["queued"] += 1
                if created >= 16:
                    return created
        return created

    def replenish_planners(self, conn, actor) -> int:
        """Keep one bounded planner frontier alive for each active run."""
        automation, run, assignment = (tables[name] for name in ("automation", "run", "assignment"))
        now = conn.execute(select(func.now())).scalar_one()
        created = 0
        rows = conn.execute(select(automation).join(run, automation.c.run_id == run.c.id).where(
            automation.c.enabled.is_(True), automation.c.role == "worker", run.c.status == "active",
            automation.c.functions.contains(["planner"]))).mappings()
        for rule in rows:
            if (rule.get("functions") or []) and "orchestrator" in rule["functions"]:
                continue
            active = conn.execute(select(func.count()).select_from(assignment).where(
                assignment.c.automation_id == rule["id"],
                assignment.c.status.in_(("pending", "running", "stopping")))).scalar_one()
            if active or (rule["not_before"] and rule["not_before"] > now):
                continue
            if self.replenish(conn, actor, dict(rule)) is not None:
                created += 1
        return created

    def replenish_orchestrators(self, conn, actor, now) -> int:
        """Renew bounded orchestrator episodes after their cooldown.

        The recurrence is durable in the assignment table, so a provider
        context never has to stay open merely to monitor a run.
        """
        automation, run, assignment = (tables[name] for name in ("automation", "run", "assignment"))
        rows = conn.execute(select(automation).join(run, automation.c.run_id == run.c.id).where(
            automation.c.enabled.is_(True), run.c.status == "active",
            automation.c.functions.contains(["orchestrator"]))).mappings()
        created = 0
        for rule in rows:
            run_row = get(conn, "run", rule["run_id"])
            # Persist one immutable observation per frontier/health pair. The
            # model episode receives the same bounded view through context, but
            # the durable record lets the next episode explain why a decision
            # was made without replaying an unbounded provider transcript.
            try:
                from .coordination import global_health
                from .coordination_memory import frontier
                from .health_control import (HealthIssueSpec, HealthSnapshotSpec,
                                             Scope, capture_health_snapshot,
                                             upsert_health_issue)
                import hashlib, json
                health = global_health(conn, self.service)
                frontier_hash = frontier(conn, run_row["id"])
                health_hash = hashlib.sha256(json.dumps(health, sort_keys=True, default=str).encode()).hexdigest()
                capture_health_snapshot(conn, HealthSnapshotSpec(
                    scope=Scope(project_id=project_of(conn, "run", run_row["id"]), run_id=run_row["id"]),
                    kind="run", frontier_hash=frontier_hash, health_hash=health_hash,
                    snapshot_key=f"{frontier_hash}:{health_hash}", payload=health))
                if not health.get("healthy_capacity_pools"):
                    upsert_health_issue(conn, HealthIssueSpec(
                        scope=Scope(project_id=project_of(conn, "run", run_row["id"]), run_id=run_row["id"]),
                        code="no_healthy_capacity", fingerprint=health_hash,
                        severity="error", summary="No healthy capacity is available for this run",
                        details={"status": health.get("status"), "reasons": health.get("reasons", [])}))
            except (ValueError, KeyError, DomainError):
                # Health persistence must not prevent deterministic admission;
                # the episode still sees the live derived view and can report
                # a control issue through its normal API contract.
                pass
            active = conn.execute(select(func.count()).select_from(assignment).where(
                assignment.c.automation_id == rule["id"],
                assignment.c.status.in_(("pending", "running", "stopping")))).scalar_one()
            if active:
                continue
            latest = conn.execute(select(assignment.c.finished_at).where(
                assignment.c.automation_id == rule["id"],
                assignment.c.finished_at.is_not(None)).order_by(assignment.c.finished_at.desc()).limit(1)).scalar_one_or_none()
            if latest is not None and latest + timedelta(seconds=max(1, rule["cooldown_seconds"])) > now:
                continue
            if self.replenish(conn, actor, dict(rule)) is not None:
                created += 1
        return created

    def retire_unusable_maintainers(self, conn, actor) -> int:
        """Replace recurring batches whose retained provider context is gone."""
        assignment, thread, request, run = (tables[name] for name in
                                            ("assignment", "provider_thread", "provider_request", "run"))
        current_status = select(thread.c.status).where(
            thread.c.assignment_id == assignment.c.id, thread.c.kind == "primary")\
            .order_by(thread.c.number.desc()).limit(1).scalar_subquery()
        rows = list(conn.execute(select(assignment).join(run, assignment.c.run_id == run.c.id).where(
            assignment.c.role == "maintainer",
            assignment.c.status == "pending", run.c.status == "active",
            current_status.not_in(("creating", "available")),
            ~select(request.c.id).join(thread, request.c.provider_thread_id == thread.c.id).where(
                thread.c.assignment_id == assignment.c.id,
                request.c.status.in_(("pending", "submitted", "running", "uncertain"))).exists(),
        ).order_by(assignment.c.queue_rank, assignment.c.id).limit(self.CLAIM_SCAN_LIMIT)).mappings())
        for row in rows:
            self.cancel(conn, actor, dict(row),
                        "Retained maintainer context is unavailable; a fresh bounded batch will replace it",
                        disable_automation=False, reconsider=False)
        retired = len(rows)
        # A provider thread in ``creating`` has never acquired native state.
        # If its workspace host is stale or in storage pressure, keeping that
        # thread open permanently pins the assignment to an unusable machine.
        # Close only this pre-launch state; an available/retained native thread
        # still carries useful context and must remain host-affine.
        pending, thread, workspace, run_host = (tables[name] for name in
                                                ("assignment", "provider_thread", "workspace", "run_host"))
        candidates = list(conn.execute(select(thread.c.id.label("thread_id"),
            pending.c.id.label("assignment_id"), workspace.c.id.label("workspace_id"),
            workspace.c.host_id).select_from(
            pending.join(thread, thread.c.assignment_id == pending.c.id).join(
                workspace, workspace.c.id == thread.c.workspace_id).join(
                run_host, run_host.c.run_id == pending.c.run_id)).where(
            pending.c.status == "pending", pending.c.role.in_(("worker", "maintainer")),
            thread.c.kind == "primary", thread.c.status == "creating",
            run_host.c.host_id == workspace.c.host_id,
            run_host.c.enabled.is_(True),
        ).order_by(pending.c.queue_rank, pending.c.id).limit(self.CLAIM_SCAN_LIMIT)).mappings())
        for row in candidates:
            capacity = self.capacity(conn, [row["host_id"]])
            occupant = conn.execute(select(tables["execution"].c.id, tables["execution"].c.assignment_id).where(
                tables["execution"].c.workspace_id == row["workspace_id"],
                self.workspace_busy_condition()).limit(1)).first()
            # A pre-launch context has no useful provider state.  If its
            # workspace is occupied, closing only the thread leaves the
            # assignment permanently unadmittable because available_workspace
            # rejects any historical primary thread.  Cancel the stale entry
            # so its automation or planner can create a fresh placement.
            # Its own lost execution remains fenced until physical stop is
            # confirmed; retain the assignment for normal lease recovery.
            if occupant and occupant.assignment_id == row["assignment_id"]:
                continue
            if capacity and not occupant:
                continue
            assignment_row = get(conn, "assignment", row["assignment_id"])
            self.cancel(conn, actor, assignment_row,
                        "Pre-launch context is stale or its workspace is occupied; retry with fresh placement",
                        disable_automation=False, reconsider=False)
            retired += 1
        return retired

    def claim(self, conn, actor: Actor, host_id: UUID, harness_ids: list[UUID]) -> dict | None:
        require_host(conn, actor, host_id)
        host = get(conn, "host", host_id, lock=True)
        if host["mode"] != "enabled":
            return None
        change(conn, "host", host_id, heartbeat_at=func.now())
        from .storage import pressure
        if pressure(self.config.state_root, self.config.storage)["status"] == "pause":
            return None
        now = conn.execute(select(func.now())).scalar_one()
        # Goal updates are worker-delivered.  Do this small reconciliation on
        # every admission poll so consumed or terminal goals cannot leave a
        # permanent pending-delivery backlog.
        self.service.reconcile_goal_updates(conn)
        capacity = [row for row in self.capacity(conn, [host_id]) if row["harness_id"] in harness_ids]
        assignment, run, run_host, execution = (tables[name] for name in ("assignment", "run", "run_host", "execution"))
        # Oldest last admission first gives active runs fair access to the common pool.
        last_claim = select(assignment.c.run_id, func.max(execution.c.created_at).label("admitted_at")).select_from(
            execution.join(assignment, execution.c.assignment_id == assignment.c.id)
            .join(run, assignment.c.run_id == run.c.id)
            .join(run_host, run_host.c.run_id == run.c.id)).where(
                run_host.c.host_id == host_id, run_host.c.enabled.is_(True),
                run.c.status.in_(("active", "draining"))).group_by(assignment.c.run_id).cte(
                    "run_last_admission").prefix_with("MATERIALIZED")
        recovery_ready = and_(assignment.c.retry_at.is_not(None), assignment.c.retry_at <= now)
        last_profile = conn.execute(select(assignment.c.functions).join(execution,
            execution.c.assignment_id == assignment.c.id).where(execution.c.host_id == host_id)
            .order_by(execution.c.created_at.desc(), execution.c.id.desc()).limit(1)).scalar_one_or_none()
        supervisor_turn = "orchestrator" not in (last_profile or [])
        candidate_query = select(assignment).select_from(assignment.join(run,
            assignment.c.run_id == run.c.id).join(run_host, run_host.c.run_id == run.c.id)
            .outerjoin(last_claim, last_claim.c.run_id == run.c.id)).where(
            run_host.c.host_id == host_id, run_host.c.enabled.is_(True), run.c.status.in_(("active", "draining")),
            assignment.c.status == "pending",
            or_(assignment.c.not_before.is_(None), assignment.c.not_before <= now),
            or_(assignment.c.retry_at.is_(None), assignment.c.retry_at <= now),
            or_(assignment.c.expires_at.is_(None), assignment.c.expires_at > now),
        ).order_by(case((recovery_ready, 0), (assignment.c.start_condition.is_(None), 1), else_=2),
                    assignment.c.retry_at.asc().nullsfirst(),
                    last_claim.c.admitted_at.asc().nullsfirst(), run.c.id,
                    assignment.c.queue_rank, assignment.c.id)
        observations = {}
        # Scan both profiles so a blocked preferred page cannot hide the other
        # profile. Rejected pages rotate across polls, including within a profile.
        for supervisory in (supervisor_turn, not supervisor_turn):
            key = (host_id, supervisory)
            offset = self._claim_scan_offsets.get(key, 0)
            profile = assignment.c.functions.contains(["orchestrator"])
            candidates = list(conn.execute(candidate_query.where(profile if supervisory else ~profile)
                .offset(offset).limit(self.CLAIM_SCAN_LIMIT)).mappings())
            for candidate in candidates:
                item = dict(candidate)
                # Retain the current-head gate for legacy forge_open_count rows.
                if not self.service.readiness(conn, item, now, observations=observations).ready:
                    continue
                request, thread = tables["provider_request"], tables["provider_thread"]
                uncertain = conn.execute(select(request.c.id).select_from(request.join(thread,
                    request.c.provider_thread_id == thread.c.id)).where(thread.c.assignment_id == item["id"],
                    request.c.status.in_(("pending", "submitted", "running", "uncertain"))).limit(1)).first()
                if uncertain:
                    continue
                campaign = get(conn, "run", item["run_id"])
                for slot in capacity:
                    if item["harness_id"] and item["harness_id"] != slot["harness_id"]:
                        continue
                    self.retire_orphaned_prelaunch_threads(conn, actor, item["id"], host_id)
                    self.retire_failed_primary_contexts(conn, actor, item["id"])
                    previous = self.retained_thread_for_host(conn, item["id"], host_id)
                    if previous:
                        pinned = get(conn, "record_revision", previous["harness_revision_id"])
                        if UUID(pinned["content"]["id"]) != slot["harness_id"]:
                            continue
                    busy = conn.execute(select(func.count()).select_from(execution).where(
                        execution.c.host_id == host_id, execution.c.harness_id == slot["harness_id"],
                        self.execution_busy_condition())).scalar_one()
                    if busy >= slot["execution_slots"] or not self.limits_available(conn, slot["limits"]):
                        continue
                    workspace = self.available_workspace(conn, item, host_id)
                    if workspace is None:
                        continue
                    if not previous and self.prefer_other_workspace_host(conn, item, workspace, observations):
                        continue
                    grant = self.start(conn, actor, item, campaign, slot, host, workspace, now)
                    self._claim_scan_offsets[key] = 0
                    return grant
            self._claim_scan_offsets[key] = offset + len(candidates) if len(candidates) == self.CLAIM_SCAN_LIMIT else 0
        return None

    def prefer_other_workspace_host(self, conn, assignment, workspace, cache):
        """Leave a retained worktree idle when another host can start independently."""
        def retained(identifier):
            key = ("placement_retained", identifier)
            if key not in cache:
                cache[key] = conn.execute(select(tables["workspace"].c.id).where(
                    tables["workspace"].c.id == identifier,
                    tables["workspace"].c.id.in_(self.retained_workspaces()))).first() is not None
            return cache[key]

        if workspace["status"] != "preparing" and not retained(workspace["id"]):
            return False
        run_id = assignment["run_id"]
        key = ("placement_capacity", run_id)
        if key not in cache:
            run_host = tables["run_host"]
            hosts = list(conn.execute(select(run_host.c.host_id).where(
                run_host.c.run_id == run_id, run_host.c.enabled.is_(True))).scalars())
            cache[key] = self.capacity(conn, hosts)
        execution = tables["execution"]
        for slot in cache[key]:
            if slot["host_id"] == workspace["host_id"] or (
                    assignment["harness_id"] and assignment["harness_id"] != slot["harness_id"]):
                continue
            slot_key = ("placement_slot", slot["host_id"], slot["harness_id"])
            if slot_key not in cache:
                busy = conn.execute(select(func.count()).select_from(execution).where(
                    execution.c.host_id == slot["host_id"], execution.c.harness_id == slot["harness_id"],
                    self.execution_busy_condition())).scalar_one()
                cache[slot_key] = busy < slot["execution_slots"] and self.limits_available(conn, slot["limits"])
            if not cache[slot_key]:
                continue
            workspace_key = ("placement_workspace", run_id, slot["host_id"])
            if workspace_key not in cache:
                cache[workspace_key] = self.available_workspace(conn, assignment, slot["host_id"])
            alternative = cache[workspace_key]
            if alternative and alternative["status"] == "ready" and not retained(alternative["id"]):
                return True
        return False

    def limits_available(self, conn, limits: list[dict]) -> bool:
        claim = tables["resource_claim"]
        for limit in limits:
            used = conn.execute(select(func.coalesce(func.sum(claim.c.units), 0)).where(
                claim.c.resource_limit_id == limit["id"], claim.c.released_at.is_(None))).scalar_one()
            # After a provider outage, admit one real request as the recovery probe.
            maximum = 1 if limit["failure_count"] else limit["max_concurrent"]
            if used >= maximum:
                return False
        return True

    def admission_blocker(self, conn, assignment, *, observations=None):
        """Explain physical admission separately from an assignment's logical condition."""
        cache = observations if observations is not None else {}
        if "admission_storage_pressure" not in cache:
            from .storage import pressure
            cache["admission_storage_pressure"] = pressure(self.config.state_root, self.config.storage)["status"]
        if cache["admission_storage_pressure"] == "pause":
            return "Admission paused by configured control-plane storage thresholds"
        run_id = assignment["run_id"]
        key = ("admission_capacity", run_id)
        if key not in cache:
            run_host = tables["run_host"]
            hosts = list(conn.execute(select(run_host.c.host_id).where(
                run_host.c.run_id == run_id, run_host.c.enabled.is_(True))).scalars())
            cache[key] = self.capacity(conn, hosts)
        slots = cache[key]
        request, thread = tables["provider_request"], tables["provider_thread"]
        if "admission_unsettled" in cache:
            unsettled = assignment["id"] in cache["admission_unsettled"]
        else:
            unsettled = conn.execute(select(request.c.id).join(thread,
                request.c.provider_thread_id == thread.c.id).where(thread.c.assignment_id == assignment["id"],
                request.c.status.in_(("pending", "submitted", "running", "uncertain"))).limit(1)).first()
        if unsettled:
            return "Waiting for a previous provider request to be reconciled"
        if "admission_threads" in cache:
            previous = cache["admission_threads"].get(assignment["id"])
        else:
            previous = conn.execute(select(thread).where(thread.c.assignment_id == assignment["id"],
                thread.c.kind == "primary").order_by(thread.c.created_at.desc()).limit(1)).mappings().first()
        harness_id = assignment["harness_id"]
        if previous:
            if previous["status"] not in ("creating", "available"):
                return "Retained session context is unavailable; explicit recovery is required"
            workspace = get(conn, "workspace", previous["workspace_id"])
            pinned = get(conn, "record_revision", previous["harness_revision_id"])
            harness_id = UUID(pinned["content"]["id"])
            slots = [slot for slot in slots if slot["host_id"] == workspace["host_id"]]
            if not slots:
                return "Retained session requires its original host, which is offline, disabled or cooling down"
            execution, owner = tables["execution"], tables["assignment"]
            occupant = conn.execute(select(owner.c.number, owner.c.run_id, execution.c.status,
                execution.c.stop_confirmed_at).select_from(execution.join(owner)).where(
                    execution.c.workspace_id == workspace["id"], self.workspace_busy_condition())
                .order_by(execution.c.created_at.desc()).limit(1)).mappings().first()
            if occupant:
                host = get(conn, "host", workspace["host_id"])
                run = get(conn, "run", occupant["run_id"])
                state = ("is using" if occupant["status"] in LIVE_EXECUTIONS else
                         "has not released")
                return (f"Retained workspace on {host['slug']}: R{run['number']}/A{occupant['number']} "
                        f"{state} this workspace; free slots on other hosts cannot resume this context")
            if workspace["status"] != "ready" and not self.workspace_preparation(conn, assignment, workspace):
                host = get(conn, "host", workspace["host_id"])
                return f"Retained workspace on {host['slug']} is {workspace['status']}; verification is required"
        slots = [slot for slot in slots if not harness_id or slot["harness_id"] == harness_id]
        if not slots:
            return "No healthy enabled host has a compatible harness available; check host health, storage and cooldowns"
        execution = tables["execution"]
        blockers = []
        for slot in slots:
            slot_key = ("admission_slot", slot["host_id"], slot["harness_id"])
            if slot_key not in cache:
                busy = conn.execute(select(func.count()).select_from(execution).where(
                    execution.c.host_id == slot["host_id"], execution.c.harness_id == slot["harness_id"],
                    self.execution_busy_condition())).scalar_one()
                cache[slot_key] = {"busy": busy, "limits_available": self.limits_available(conn, slot["limits"])}
            availability = cache[slot_key]
            host = get(conn, "host", slot["host_id"])
            if availability["busy"] >= slot["execution_slots"]:
                blockers.append(f"{host['slug']}: all {slot['execution_slots']} compatible execution slots are occupied")
            elif not availability["limits_available"]:
                blockers.append(f"{host['slug']}: shared provider or resource concurrency limit is reached")
            else:
                workspace_key = ("admission_workspace", run_id, slot["host_id"], previous["workspace_id"] if previous else None)
                if workspace_key not in cache:
                    cache[workspace_key] = self.available_workspace(conn, assignment, slot["host_id"]) is not None
                if cache[workspace_key]:
                    return None
                blockers.append(f"{host['slug']}: no verified, reconciled workspace is free of active or unsettled executions")
        return "; ".join(dict.fromkeys(blockers))

    @staticmethod
    def admission_observations(conn, rows):
        identifiers = [row["id"] for row in rows if row["status"] == "pending"]
        if not identifiers:
            return {}
        request, thread = tables["provider_request"], tables["provider_thread"]
        threads = {row["assignment_id"]: dict(row) for row in conn.execute(select(thread).where(
            thread.c.assignment_id.in_(identifiers), thread.c.kind == "primary").order_by(thread.c.created_at)).mappings()}
        unsettled = set(conn.execute(select(thread.c.assignment_id).join(request,
            request.c.provider_thread_id == thread.c.id).where(thread.c.assignment_id.in_(identifiers),
            request.c.status.in_(("pending", "submitted", "running", "uncertain")))).scalars())
        return {"admission_threads": threads, "admission_unsettled": unsettled}

    def budget_exhausted(self, conn, run: dict) -> bool:
        if run["token_budget"] is None:
            return False
        from .admission import run_usage
        return run_usage(conn, run)["observed_tokens"] >= run["token_budget"]

    @staticmethod
    def execution_busy_condition(*, grace_seconds: int = 300):
        execution, request = tables["execution"], tables["provider_request"]
        unsettled = select(request.c.execution_id).where(
            request.c.status.in_(("pending", "submitted", "running", "uncertain")))
        # A lost execution remains physically uncertain for a short grace
        # period, but a missing stop confirmation must never reserve a slot
        # forever. Outstanding provider requests remain a hard reservation.
        uncertain_stop = and_(execution.c.status.not_in(LIVE_EXECUTIONS),
            execution.c.stop_confirmed_at.is_(None),
            func.coalesce(execution.c.finished_at, execution.c.started_at) >
                func.now() - timedelta(seconds=grace_seconds))
        return or_(execution.c.status.in_(LIVE_EXECUTIONS), uncertain_stop,
                   execution.c.id.in_(unsettled))

    @staticmethod
    def workspace_busy_condition():
        return Scheduler.execution_busy_condition()

    @staticmethod
    def retained_workspaces():
        thread, owner = tables["provider_thread"], tables["assignment"]
        return select(thread.c.workspace_id).join(owner, owner.c.id == thread.c.assignment_id).where(
            thread.c.kind == "primary", thread.c.status.in_(("creating", "available")),
            owner.c.status.in_(("pending", "running", "stopping")))

    @staticmethod
    def can_prepare_workspace(host):
        capabilities = (host.get("health") or {}).get("capabilities", {})
        version = capabilities.get("workspace_preparation") if isinstance(capabilities, dict) else None
        return type(version) is int and version == 1

    def workspace_preparation(self, conn, assignment, workspace):
        host = get(conn, "host", workspace["host_id"])
        expected_path = str(PurePosixPath(host["workspace_root"]) / "assignments" / str(assignment["id"]))
        if (not self.can_prepare_workspace(host) or workspace["status"] != "preparing"
                or workspace["path"] != expected_path
                or workspace["branch_name"] != "horizon/assignments/" + str(assignment["id"])):
            return None
        table = tables["workspace"]
        source = conn.execute(select(table).where(table.c.host_id == host["id"],
            table.c.repository_id == workspace["repository_id"], table.c.project_id == workspace["project_id"],
            table.c.status == "ready", table.c.id != workspace["id"]).order_by(table.c.created_at, table.c.id)
            .limit(1)).mappings().first()
        if source is None:
            return None
        return {"schema_version": 1, "source_workspace_id": str(source["id"]), "source_path": source["path"],
                "commit_oid": workspace["base_commit_oid"], "branch_name": workspace["branch_name"]}

    def available_workspace(self, conn, assignment, host_id):
        thread, workspace, execution = (tables[name] for name in ("provider_thread", "workspace", "execution"))
        latest = conn.execute(select(thread.c.status).where(
            thread.c.assignment_id == assignment["id"], thread.c.kind == "primary")
            .order_by(thread.c.number.desc()).limit(1)).scalar_one_or_none()
        if latest not in (None, "creating", "available"):
            return None
        previous = self.retained_thread_for_host(conn, assignment["id"], host_id)
        if not previous and self.has_native_foreign_thread(conn, assignment["id"], host_id):
            return None
        busy = select(execution.c.workspace_id).where(self.workspace_busy_condition())
        host = get(conn, "host", host_id)
        capable = self.can_prepare_workspace(host)
        query = select(workspace).where(workspace.c.host_id == host_id,
            workspace.c.project_id == project_of(conn, "assignment", assignment["id"]), workspace.c.id.not_in(busy))
        if previous:
            row = conn.execute(query.where(workspace.c.id == previous["workspace_id"])).mappings().first()
            if row and (row["status"] == "ready" or self.workspace_preparation(conn, assignment, row)):
                return dict(row)
            return None
        else:
            repository = tables["repository"]
            query = query.join(repository).where(repository.c.purpose == "workspace", workspace.c.status == "ready")
            if capable:
                query = query.where(workspace.c.id.not_in(self.retained_workspaces()))
            # Older workers cannot provision directories; retain their placement
            # preference until their advertised capabilities have been upgraded.
            query = query.order_by(workspace.c.id.in_(self.retained_workspaces()))
        row = conn.execute(query.order_by(workspace.c.id).limit(1)).mappings().first()
        if row or not capable:
            return dict(row) if row else None
        repository = tables["repository"]
        campaign = get(conn, "run", assignment["run_id"])
        phase = campaign["phase"]
        source_query = select(workspace).join(repository).where(workspace.c.host_id == host_id,
            workspace.c.project_id == project_of(conn, "assignment", assignment["id"]), workspace.c.status == "ready",
            repository.c.purpose == "workspace")
        if phase["kind"] == "postprocessing":
            pinned_source = get(conn, "workspace", phase["source_workspace_id"])
            source_query = source_query.where(workspace.c.repository_id == pinned_source["repository_id"])
        source = conn.execute(source_query.order_by(workspace.c.created_at, workspace.c.id).limit(1)).mappings().first()
        if source is None:
            return None
        base = (phase["source_commit_oid"] if phase["kind"] == "postprocessing"
                else source["head_commit_oid"] or source["base_commit_oid"])
        # This is a read-only allocation proposal. Only an actual claim persists
        # the directory, so dashboard admission checks cannot create workspaces.
        return {"id": uuid5(assignment["id"], "workspace:" + str(host_id)), "project_id": source["project_id"],
                "host_id": host_id, "repository_id": source["repository_id"],
                "path": str(PurePosixPath(host["workspace_root"]) / "assignments" / str(assignment["id"])),
                "branch_name": "horizon/assignments/" + str(assignment["id"]),
                "base_commit_oid": base, "head_commit_oid": None, "status": "preparing"}

    def start(self, conn, actor, item, run, slot, host, workspace, now):
        from .root_maintenance import admitted
        admitted(conn, item, now)
        if workspace["status"] == "preparing" and "revision" not in workspace:
            workspace = create(conn, "workspace", **workspace)
            emit(conn, actor.id, workspace["project_id"], "workspace", workspace, ["status", "path"])
        preparation = self.workspace_preparation(conn, item, workspace) if workspace["status"] == "preparing" else None
        mission = get(conn, "mission", item["mission_id"])
        harness = get(conn, "harness", slot["harness_id"])
        thread_table = tables["provider_thread"]
        thread = self.retained_thread_for_host(conn, item["id"], host["id"])
        project_id = mission["project_id"]
        assignment_revision = snapshot(conn, "assignment", item, actor.id)
        mission_revision = snapshot(conn, "mission", mission, actor.id)
        from .reviewer_invocations import assignment_manifest
        review = assignment_manifest(conn, self.service, item) if item["reviewer_descriptor_id"] else None
        manifest = review[1] if review else None
        harness_revision = (thread["harness_revision_id"] if thread else
            UUID(manifest["harness_revision_id"]) if manifest else snapshot(conn, "harness", harness, actor.id))
        from .prompts import catalog, goal
        bundle = (get(conn, "artifact", thread["skill_bundle_artifact_id"]) if thread else
            get(conn, "artifact", UUID(manifest["skill_bundle_artifact_id"])) if manifest else
            save_blob(conn, self.service.store, project_id, catalog(self.config.skill_source_root)))
        sandbox = save_blob(conn, self.service.store, project_id, host["sandbox"])
        lease_until = now + timedelta(seconds=self.config.lease_seconds)
        execution = create(conn, "execution", assignment_id=item["id"], created_at=func.clock_timestamp(),
            number=next_number(conn, "execution", "assignment_id", item["id"]), host_id=host["id"],
            workspace_id=workspace["id"], harness_id=harness["id"],
            assignment_revision_id=assignment_revision, mission_revision_id=mission_revision,
            harness_revision_id=harness_revision, skill_bundle_artifact_id=bundle["id"],
            sandbox_manifest_artifact_id=sandbox["id"], roadmap_snapshot_id=run["adopted_roadmap_snapshot_id"],
            lease_expires_at=lease_until, started_at=now, heartbeat_at=now, status="running")
        for limit in slot["limits"]:
            create(conn, "resource_claim", resource_limit_id=limit["id"], execution_id=execution["id"], units=1)
        changed = change(conn, "assignment", item["id"], status="running", started_at=item["started_at"] or now,
                         retry_at=None, finished_at=None, status_note=None, checkpoint_requested_at=None)
        if not thread:
            thread = create(conn, "provider_thread", assignment_id=item["id"],
                number=next_number(conn, "provider_thread", "assignment_id", item["id"]),
                workspace_id=workspace["id"], harness_revision_id=harness_revision,
                skill_bundle_artifact_id=bundle["id"], provider_state_ref=f"assignments/{item['id']}/provider",
                applied_model_options=manifest["model_options"] if manifest else
                    {**harness["model_options"], **item["model_options"]})
        principal = create(conn, "principal", kind="agent", display_name=f"R{run['number']}/A{item['number']}",
                           execution_id=execution["id"])
        _, token = issue_credential(conn, principal["id"], "execution_token", "Execution lease",
                                    lease_until)
        emit(conn, actor.id, project_id, "assignment", changed, ["status", "started_at"], previous="pending")
        prompt = goal(conn, self.service, item, run, mission, initial=thread["provider_thread_id"] is None)
        goal_artifact = save_blob(conn, self.service.store, project_id, {"goal": prompt}, execution["id"]) if len(prompt.encode()) > 6000 else None
        if goal_artifact:
            from sqlalchemy.dialects.postgresql import insert
            conn.execute(insert(tables["assignment_artifact"]).values(assignment_id=item["id"],
                artifact_id=goal_artifact["id"]).on_conflict_do_nothing())
        return {"execution_id": str(execution["id"]), "assignment_id": str(item["id"]), "epoch": execution["number"],
                "lease_seconds": self.config.lease_seconds, "harness_id": str(harness["id"]),
                "max_parallel_subagents": slot["max_parallel_subagents"],
                "sandbox_manifest": host["sandbox"],
                "max_offline_replay_seconds": self.config.storage.max_offline_replay_seconds,
                "workspace_id": str(workspace["id"]), "workspace_path": workspace["path"],
                **({"workspace_preparation": preparation} if preparation else {}),
                "repository_id": str(workspace["repository_id"]), "goal": "" if goal_artifact else prompt,
                "goal_artifact_id": str(goal_artifact["id"]) if goal_artifact else None,
                "skill_bundle_sha256": bundle["content"]["sha256"],
                "provider_thread_record_id": str(thread["id"]), "provider_thread_id": thread["provider_thread_id"],
                "mission_revision_id": str(mission_revision), "mission_revision_number": mission["revision"],
                "run_revision": run["revision"],
                "role": item["role"], "functions": list(item["functions"] or []),
                "roadmap_snapshot_id": str(run["adopted_roadmap_snapshot_id"]) if run["adopted_roadmap_snapshot_id"] else None,
                "harness_configuration": {**get(conn, "record_revision", harness_revision)["content"],
                                           "model_options": thread["applied_model_options"]},
                "execution_token": token}

    def heartbeat(self, conn, actor: Actor, execution_id: UUID, epoch: int) -> dict:
        execution = get(conn, "execution", execution_id, lock=True)
        require_host(conn, actor, execution["host_id"])
        now = conn.execute(select(func.now())).scalar_one()
        if execution["number"] != epoch or execution["status"] not in LIVE_EXECUTIONS or execution["lease_expires_at"] <= now:
            raise DomainError("stale_epoch", "Execution lease is no longer valid")
        item = get(conn, "assignment", execution["assignment_id"])
        run = get(conn, "run", item["run_id"])
        stop = (execution["status"] == "stopping" or item["status"] == "stopping"
                or run["status"] in ("stopping", "cancelled")
                or bool(item["expires_at"] and item["expires_at"] <= now))
        change(conn, "host", execution["host_id"], heartbeat_at=now)
        if stop:
            return {"lease_seconds": max(0, (execution["lease_expires_at"] - now).total_seconds()), "stop": True}
        change(conn, "execution", execution_id, heartbeat_at=now,
               lease_expires_at=now + timedelta(seconds=self.config.lease_seconds))
        principal, credential = tables["principal"], tables["credential"]
        conn.execute(update(credential).where(credential.c.principal_id.in_(select(principal.c.id).where(
            principal.c.execution_id == execution_id)), credential.c.kind == "execution_token",
            credential.c.revoked_at.is_(None)).values(expires_at=now + timedelta(seconds=self.config.lease_seconds)))
        from .prompts import goal
        mission = get(conn, "mission", item["mission_id"])
        thread, request = tables["provider_thread"], tables["provider_request"]
        requests = list(conn.execute(select(request).select_from(request.join(thread,
            request.c.provider_thread_id == thread.c.id)).where(thread.c.assignment_id == item["id"],
            thread.c.kind == "primary").order_by(request.c.created_at.desc()).limit(run["retry_policy"]["max_no_progress_requests"] + 1)).mappings())
        latest = requests[0] if requests else None
        findings = self.service.completion_findings(conn, item["id"])
        # A maintainer execution is a bounded review batch.  A fresh batch is
        # cheaper to reason about than extending one ever-growing provider
        # context, and any remaining findings are handed to its successor.
        continue_request = bool(item["role"] != "maintainer" and latest and latest["status"] == "completed" and findings)
        no_progress = self.service.progress_stalled(conn, item["id"], run["retry_policy"]["max_no_progress_requests"])
        checkpoint = bool(findings and item["checkpoint_requested_at"] and latest and latest["status"] == "completed"
                          and not self.service.settlement_findings(conn, item["id"], include_pending_deliveries=False))
        result = {"lease_seconds": self.config.lease_seconds, "stop": False,
                "continue": continue_request and not no_progress and not checkpoint,
                "yield": checkpoint or (continue_request and no_progress),
                "mission_revision_number": mission["revision"]}
        if latest and latest["status"] == "completed":
            result["goal"] = goal(conn, self.service, item, run, mission, findings=findings, initial=False)
            result["mission_revision_id"] = str(snapshot(conn, "mission", mission, actor.id))
            result["run_revision"] = run["revision"]
            result["roadmap_snapshot_id"] = str(run["adopted_roadmap_snapshot_id"]) if run["adopted_roadmap_snapshot_id"] else None
        return result

    def finish(self, conn, actor: Actor, execution: dict, status: str, failure: dict | None = None,
               *, journal_pending: bool = False, reason: str | None = None) -> dict:
        # Older workers classified their own execution budget as a network
        # timeout. Retain the local retry without cooling the shared account.
        if failure and failure.get("code") == "timeout" and failure.get("message") == (
                "Provider request exceeded its execution deadline"):
            failure = {**failure, "kind": "execution", "code": "request_deadline"}
        item = get(conn, "assignment", execution["assignment_id"], lock=True)
        run = get(conn, "run", item["run_id"])
        from .root_maintenance import NAME as ROOT_MAINTAINER
        automation = get(conn, "automation", item["automation_id"]) if item["automation_id"] else None
        root_maintainer = bool(automation and automation["name"] == ROOT_MAINTAINER)
        now = conn.execute(select(func.now())).scalar_one()
        if execution["status"] not in LIVE_EXECUTIONS:
            return item
        stopping = item["status"] == "stopping" or run["status"] in ("stopping", "cancelled")
        execution_status = "cancelled" if stopping or status == "cancelled" else (
            "succeeded" if status in ("succeeded", "yielded") else status)
        confirmed = execution.get("stop_confirmed_at")
        if actor.kind == "host" and status != "lost":
            confirmed = now
        change(conn, "execution", execution["id"], status=execution_status, finished_at=now,
               lease_expires_at=now, failure=failure, stop_confirmed_at=confirmed)
        claim = tables["resource_claim"]
        limits = tables["resource_limit"]
        provider_limits = list(conn.execute(select(limits).where(limits.c.kind == "provider_account",
            limits.c.id.in_(select(claim.c.resource_limit_id).where(claim.c.execution_id == execution["id"],
                claim.c.released_at.is_(None))))).mappings())
        code = (failure or {}).get("code")
        if code in {"rate_limited", "provider_overloaded", "connection_error", "timeout"}:
            for limit in provider_limits:
                failures = limit["failure_count"] + 1
                policy = run["retry_policy"]
                delay = min(policy["max_delay_seconds"], policy["initial_delay_seconds"] * 2 ** min(failures - 1, 20))
                until = now + timedelta(seconds=random.uniform(delay / 2, delay))
                change(conn, "resource_limit", limit["id"], failure_count=failures,
                       cooldown_until=max(until, limit["cooldown_until"]) if limit["cooldown_until"] else until)
        elif status in ("succeeded", "yielded"):
            request = tables["provider_request"]
            healthy = conn.execute(select(request.c.id).where(request.c.execution_id == execution["id"],
                request.c.status == "completed").limit(1)).first()
            if healthy:
                for limit in provider_limits:
                    # An older in-flight request must not clear a newer failure's backoff.
                    if not limit["cooldown_until"] or execution["started_at"] >= limit["cooldown_until"]:
                        change(conn, "resource_limit", limit["id"], failure_count=0, cooldown_until=None)
        if confirmed is not None:
            conn.execute(update(claim).where(claim.c.execution_id == execution["id"], claim.c.released_at.is_(None)).values(released_at=now))
        principal, credential = tables["principal"], tables["credential"]
        conn.execute(update(credential).where(credential.c.principal_id.in_(select(principal.c.id).where(
            principal.c.execution_id == execution["id"]))).values(revoked_at=now))
        values = {"finished_at": now, "retry_at": None}
        if stopping or status == "cancelled":
            values["status"] = "cancelled"
        elif status == "yielded" and reason in ("execution_budget_reached", "request_budget_reached"):
            # A new lease must not reset the budget of the same unfinished
            # context. Preserve its ledger and let reconsideration own recovery.
            findings = self.service.completion_findings(conn, item["id"])
            unfinished = journal_pending or bool(findings)
            values.update(status="failed" if unfinished else "completed",
                          checkpoint_requested_at=None, start_condition=None, not_before=None,
                          status_note=("Execution episode budget exhausted; preserved work requires explicit "
                                       "replanning or recovery" if unfinished else "Bounded deliverable complete"))
        elif journal_pending and status in ("yielded", "succeeded"):
            values.update(status="pending", finished_at=None, not_before=now + timedelta(seconds=60),
                          status_note="Local API intent reconciliation remains outstanding; preserved context will resume")
        elif status in ("yielded", "succeeded") and self.service.pending_deliveries(conn, item["id"]):
            # The outbox owns retries. Release model capacity while preserving the
            # assignment and its provider context until delivery supplies a result.
            values.update(status="pending", finished_at=None, checkpoint_requested_at=now,
                          status_note="Checkpointed; waiting for durable Forge or Zulip delivery")
        elif (status in ("yielded", "succeeded") and item["role"] == "worker"
              and "planner" not in item["functions"]
              and not item["checkpoint_requested_at"]
              and not self.service.pending_deliveries(conn, item["id"])
              and not self.service.settlement_findings(conn, item["id"], include_pending_deliveries=False)
              and self._verified_publication(conn, item["id"])):
            findings = self.service.completion_findings(conn, item["id"])
            handed_off = not findings or self._handoff_worker_findings(conn, actor, item, findings)
            values.update(status="completed" if handed_off else "pending", checkpoint_requested_at=None,
                          start_condition=None, not_before=None,
                          status_note=("Unresolved work has no live maintenance owner; recovery required"
                                       if not handed_off else "Published bounded deliverable; follow-up has a maintenance owner"
                                       if findings else None))
            if not handed_off:
                values.update(finished_at=None, not_before=now + timedelta(seconds=60))
        elif (status in ("yielded", "succeeded") and "orchestrator" in item["functions"]
              and not self.service.settlement_findings(conn, item["id"], include_pending_deliveries=False)):
            # Orchestration is an episode, never a retained provider context.
            # An episode that failed to account for its own control obligation
            # is recorded as failed so the next episode gets a fresh context and
            # the outstanding obligation remains visible for recovery.
            findings = self.service.completion_findings(conn, item["id"])
            values.update(status="completed" if not findings else "failed",
                          checkpoint_requested_at=None, start_condition=None,
                          not_before=None,
                          status_note="; ".join(findings) if findings else "Bounded orchestration episode complete")
        elif (status in ("yielded", "succeeded") and item["role"] == "maintainer"
              and not root_maintainer
              and not item["checkpoint_requested_at"]
              and not self.service.settlement_findings(conn, item["id"], include_pending_deliveries=False)):
            findings = self.service.completion_findings(conn, item["id"])
            handed_off = not findings or self._handoff_worker_findings(conn, actor, item, findings)
            values.update(status="completed" if handed_off else "pending", checkpoint_requested_at=None,
                          start_condition=None, not_before=None,
                          status_note="Bounded review complete; unresolved work has a maintenance owner" if findings and handed_off
                          else "Unresolved review has no live maintenance owner; recovery required" if findings
                          else "Bounded maintainer batch complete")
            if not handed_off:
                values.update(finished_at=None, not_before=now + timedelta(seconds=60))
        elif status in ("yielded", "succeeded") and not self.service.completion_findings(conn, item["id"]):
            # A previous wait request must not retain an already delivered task.
            values.update(status="completed", checkpoint_requested_at=None,
                          start_condition=None, not_before=None, status_note=None)
        elif (status in ("yielded", "succeeded") and item["checkpoint_requested_at"]
              and not self.service.settlement_findings(conn, item["id"], include_pending_deliveries=False)
              and self.service.condition_readiness(conn, item, now, events_only=True).truth is Truth.FALSE):
            # A blocked external event releases capacity without another model
            # turn. Earlier idle turns must not destroy this legitimate wait.
            values.update(status="pending", finished_at=None,
                          status_note=item["status_note"] or "Checkpointed for external event")
        elif (status in ("yielded", "succeeded") and self.service.progress_stalled(
                conn, item["id"], run["retry_policy"]["max_no_progress_requests"])):
            # A successful provider response can still leave the same control
            # notice or failed delivery unresolved across execution leases.
            values.update(status="failed", checkpoint_requested_at=None,
                          status_note="Continuation limit reached without accounted progress; reconsideration required")
        elif item["checkpoint_requested_at"] and status in ("yielded", "succeeded"):
            values.update(status="pending", finished_at=None, status_note=item["status_note"] or "Checkpointed for continuation")
        elif status == "yielded":
            values.update(status="pending", finished_at=None, not_before=now + timedelta(seconds=60),
                          status_note="Checkpointed; waiting before continuing")
        elif status == "succeeded":
            findings = self.service.completion_findings(conn, item["id"])
            if findings:
                values.update(status="pending", finished_at=None, not_before=now + timedelta(seconds=60),
                              status_note="; ".join(findings))
            else:
                values["status"] = "completed"
        else:
            policy = run["retry_policy"]
            recoveries = item["recovery_attempts"] + 1
            code = failure["code"] if failure else "execution_failed"
            values["recovery_attempts"] = recoveries
            if code in CONFIGURATION_CODES:
                host_harness = tables["host_harness"]
                conn.execute(update(host_harness).where(host_harness.c.host_id == execution["host_id"],
                    host_harness.c.harness_id == execution["harness_id"]).values(enabled=False))
                values.update(status="pending", finished_at=None, recovery_attempts=item["recovery_attempts"],
                              status_note="Harness disabled pending configuration or storage repair")
            elif (code == "request_deadline" and item["automation_id"]
                  and run["status"] in ("active", "draining")):
                # A provider deadline is a bounded execution failure for an
                # automated owner. Replaying the same retained context is
                # especially harmful for maintainers and control episodes:
                # it can consume the whole queue while making no new
                # progress. Leave the assignment terminal and require an
                # explicit operator/planner retry after the failure is
                # understood.
                values.update(status="failed", status_note=(failure or {}).get(
                    "message", "Automated execution exceeded its provider deadline; operator review required"))
            elif code in TRANSIENT_CODES and recoveries <= policy["max_recovery_attempts"] and run["status"] in ("active", "draining"):
                cap = min(policy["max_delay_seconds"], policy["initial_delay_seconds"] * 2 ** min(recoveries - 1, 20))
                values.update(status="pending", finished_at=None, retry_at=now + timedelta(seconds=random.uniform(cap / 2, cap)),
                              status_note="Recoverable failure; preserved context will resume")
            else:
                values.update(status="failed", status_note=(failure or {}).get("message", "Execution failed"))
        row = change(conn, "assignment", item["id"], **values)
        if row["status"] == "completed" and "orchestrator" in item["functions"]:
            obligation = tables["obligation"]
            root = conn.execute(select(obligation).where(obligation.c.assignment_id == item["id"],
                obligation.c.number == 1, obligation.c.created_by_execution_id.is_(None),
                obligation.c.status == "open")).mappings().first()
            if root:
                self.service.resolve_obligation(conn, self.system_actor(conn), root["id"],
                    models.ObligationResolve(expected_revision=root["revision"], status="done",
                        resolution={"kind": "completed", "note": "The host completed this bounded control episode. "
                            "This administrative record does not complete or accept the mathematical mission.",
                            "evidence": [{"kind": "assignment", "id": str(item["id"])}]}))
        if row["status"] in ("failed", "cancelled"):
            self.reconsider(conn, actor, row)
        if item["automation_id"]:
            automation = get(conn, "automation", item["automation_id"])
            if row["status"] in ("failed", "cancelled") and not root_maintainer:
                # A terminal occurrence is a circuit breaker.  Re-enabling it
                # from this callback creates an unbounded retry loop and hides
                # a failed handoff behind fresh queue rows.  The operator or a
                # planner must explicitly reconcile the failure through
                # retry_assignment/resume_assignment (which re-enables the
                # rule after checking the same intent and current owner).
                change(conn, "automation", automation["id"], enabled=False)
            elif root_maintainer and row["status"] in ("failed", "cancelled"):
                self.replenish(conn, self.system_actor(conn), automation)
            elif row["status"] == "completed":
                if root_maintainer and run["status"] == "active" and get(conn, "mission", item["mission_id"])["status"] == "completed":
                    from .commands import Command, execute
                    campaign = get(conn, "run", item["run_id"])
                    execute(conn, self.system_actor(conn), Command(operation="drain_run", target_id=campaign["id"],
                        expected_revision=campaign["revision"],
                        args={"note": "Root maintainer recorded semantic completion; settle remaining execution and delivery"}),
                        self.service, self)
                delay = now + timedelta(seconds=automation["cooldown_seconds"])
                deferred = max(delay, automation["not_before"]) if automation["not_before"] else delay
                automation = change(conn, "automation", automation["id"], not_before=deferred)
                # Recurrence is admitted by the installation service, not a host's project grants.
                service_actor = self.system_actor(conn)
                successor = self.replenish(conn, service_actor, automation)
                if successor and item["role"] == "maintainer":
                    # Transfer unfinished ledger items to the fresh batch with
                    # an explicit scheduled resolution.  This keeps the old
                    # context terminal without dropping review decisions.
                    open_items = list(conn.execute(select(tables["obligation"]).where(
                        tables["obligation"].c.assignment_id == item["id"],
                        tables["obligation"].c.status == "open")).mappings())
                    for obligation in open_items:
                        self.service.resolve_obligation(conn, service_actor, obligation["id"],
                            models.ObligationResolve(expected_revision=obligation["revision"], status="handled",
                                resolution={"kind": "scheduled", "assignment_id": str(successor["id"]),
                                    "note": f"Fresh maintainer batch A{successor['number']} owns this follow-up."}))
        emit(conn, actor.id, project_of(conn, "assignment", row["id"]), "assignment", row,
             list(values), previous=item["status"])
        return row

    def _verified_publication(self, conn, assignment_id):
        publication = tables["publication"]
        return conn.execute(select(publication.c.id).where(
            publication.c.requested_by_assignment_id == assignment_id,
            publication.c.status == "verified").limit(1)).first() is not None

    def _handoff_worker_findings(self, conn, actor, item, findings):
        planner = tables["assignment"]
        automation = tables["automation"]
        from .root_maintenance import rule
        root_rule = rule(conn, item["run_id"])
        enabled = or_(planner.c.automation_id.is_(None), select(automation.c.id).where(
            automation.c.id == planner.c.automation_id, automation.c.enabled.is_(True)).exists())
        owner = self.root_maintenance_owner(conn, item) if root_rule else conn.execute(select(planner).where(
            planner.c.run_id == item["run_id"], planner.c.role == "worker",
            planner.c.id != item["id"],
            planner.c.functions.contains(["planner"]),
            enabled, planner.c.status == "pending").order_by(planner.c.queue_rank).limit(1)).mappings().first()
        if not owner and not root_rule:
            owner = conn.execute(select(planner).where(
                planner.c.run_id == item["run_id"], planner.c.role == "worker",
                planner.c.id != item["id"],
                planner.c.functions.contains(["planner"]), enabled, planner.c.status == "running").order_by(
                    planner.c.queue_rank).limit(1)).mappings().first()
        if not owner:
            return False
        description = (f"Assignment A{item['number']} ({item['role']}) ended with unresolved work. "
                       f"Read /api/v3/records/obligation?assignment_id={item['id']} for the original "
                       "findings and evidence before scheduling repair. Reconcile existing owners and "
                       "record the next action and its unlock condition: " + "; ".join(findings))
        handoff = create(conn, "obligation", assignment_id=owner["id"],
               number=next_number(conn, "obligation", "assignment_id", owner["id"]),
               kind="decision", description=description)
        # Replace the obligation, rather than delegating back to an ancestor
        # whose original mission may already depend on this worker.
        service_actor = self.system_actor(conn)
        for obligation in conn.execute(select(tables["obligation"]).where(
                tables["obligation"].c.assignment_id == item["id"],
                tables["obligation"].c.status == "open")).mappings():
            self.service.resolve_obligation(conn, service_actor, obligation["id"],
                models.ObligationResolve(expected_revision=obligation["revision"], status="superseded",
                    resolution={"kind": "superseded", "replacement_obligation_ids": [str(handoff["id"])],
                                "note": "Original findings retained; the linked coordination obligation owns follow-up."}))
        return True

    def root_maintenance_owner(self, conn, item):
        from .root_maintenance import rule
        automation = rule(conn, item["run_id"])
        if not automation or not automation["enabled"] or automation["id"] == item["automation_id"]:
            return None
        assignment = tables["assignment"]
        owner = conn.execute(select(assignment).where(assignment.c.automation_id == automation["id"],
            assignment.c.status.in_(("pending", "running"))).order_by(assignment.c.queue_rank).limit(1)).mappings().first()
        return dict(owner) if owner else self.replenish(conn, self.system_actor(conn), automation)

    def reconsider(self, conn, actor, assignment):
        table, obligation = tables["assignment"], tables["obligation"]
        from .root_maintenance import rule
        root_rule = rule(conn, assignment["run_id"])
        owners = self.root_maintenance_owner(conn, assignment) if root_rule else conn.execute(select(table).where(table.c.run_id == assignment["run_id"],
            table.c.id != assignment["id"], table.c.functions.contains(["planner"]),
            table.c.status.in_(("pending", "running"))).order_by(table.c.queue_rank).limit(1)).mappings().first()
        if owners:
            create(conn, "obligation", assignment_id=owners["id"],
                number=next_number(conn, "obligation", "assignment_id", owners["id"]), kind="decision",
                description=f"Reconsider unfinished work from assignment A{assignment['number']} ({assignment['id']}), which is {assignment['status']}. Inspect its preserved obligations and evidence before redispatch.")
        elif assignment["started_at"] is not None and not root_rule:
            create(conn, "obligation", assignment_id=assignment["id"],
                number=next_number(conn, "obligation", "assignment_id", assignment["id"]), kind="blocker",
                description="Reconsideration has no active planner owner. Assign a recovery owner or explicitly account for the preserved work before completing this run.")
        # Failed delegations reopen their accounting instead of silently remaining handled.
        rows = conn.execute(select(obligation).where(obligation.c.status == "handled")).mappings()
        for row in rows:
            resolution = row["resolution"] or {}
            targets = resolution.get("assignment_ids", []) + ([resolution["assignment_id"]] if "assignment_id" in resolution else [])
            if str(assignment["id"]) in targets:
                change(conn, "obligation", row["id"], status="open", resolution=None)

    def system_actor(self, conn):
        principal = tables["principal"]
        row = conn.execute(select(principal).where(principal.c.kind == "service",
            principal.c.service_name == "scheduler")).mappings().first()
        if not row:
            row = create(conn, "principal", kind="service", display_name="Horizon scheduler", service_name="scheduler")
            conn.execute(tables["system_grant"].insert().values(principal_id=row["id"], permission="administer_installation"))
        return Actor(row["id"], "service", {"service_name": "scheduler"}, "internal")

    def tick(self, conn) -> dict:
        actor = self.system_actor(conn)
        now = conn.execute(select(func.now())).scalar_one()
        execution, assignment = tables["execution"], tables["assignment"]
        # Elapsed lease-loss time cannot prove a process stopped. Its claims and
        # workspace stay reserved until a host receipt or operator fencing.
        # A confirmed host stop ends invocation ownership even if its final
        # native event was lost. Preserve uncertainty about success as interruption.
        request = tables["provider_request"]
        stopped = list(conn.execute(select(request).join(execution, execution.c.id == request.c.execution_id).where(
            request.c.status.in_(("pending", "submitted", "running", "uncertain")),
            execution.c.status.not_in(LIVE_EXECUTIONS), execution.c.stop_confirmed_at.is_not(None))
            .order_by(request.c.created_at).limit(100)).mappings())
        for row in stopped:
            settled = change(conn, "provider_request", row["id"], status="interrupted", finished_at=now,
                failure={"kind": "execution", "code": "execution_stop_confirmed",
                         "message": "Host confirmed execution stopped; no terminal invocation event was recorded"})
            from .reviewer_invocations import release_child_claims
            release_child_claims(conn, row["id"], now)
            emit(conn, actor.id, project_of(conn, "provider_request", row["id"]), "provider_request", settled,
                 ["status", "failure"], execution_id=row["execution_id"])
        recovered = 0
        for row in list(conn.execute(select(execution).where(execution.c.status.in_(LIVE_EXECUTIONS),
                execution.c.lease_expires_at <= now)).mappings()):
            request = tables["provider_request"]
            conn.execute(update(request).where(request.c.execution_id == row["id"],
                request.c.status.in_(("pending", "submitted", "running"))).values(status="uncertain"))
            self.finish(conn, actor, dict(row), "lost", {"kind": "host", "code": "lease_expired", "message": "Worker lease expired; previous ownership is fenced"})
            recovered += 1
        expired = list(conn.execute(select(assignment).where(assignment.c.status.in_(("pending", "running")),
            assignment.c.expires_at <= now)).mappings())
        for item in expired:
            self.cancel(conn, actor, dict(item), "Assignment start/end window expired")
        run = tables["run"]
        for campaign in conn.execute(select(run).where(run.c.status == "stopping")).mappings():
            busy = conn.execute(select(func.count()).select_from(assignment).where(assignment.c.run_id == campaign["id"],
                assignment.c.status.in_(("pending", "running", "stopping")))).scalar_one()
            if not busy:
                change(conn, "run", campaign["id"], status="cancelled", finished_at=now)
        from .commands import Command, execute
        completed = 0
        for campaign in list(conn.execute(select(run).where(run.c.status == "draining")).mappings()):
            try:
                # The last maintainer cannot close its own live execution. Reuse
                # the command's settlement guards after it and its deliveries stop.
                execute(conn, actor, Command(operation="complete_run", target_id=campaign["id"],
                    expected_revision=campaign["revision"],
                    args={"note": "Draining work and external deliveries have settled"}), self.service, self)
            except DomainError as error:
                if error.code not in {"mission_open", "run_unsettled"}:
                    raise
            else:
                completed += 1
        # Reconcile backlog demand independently of assignment completion.  A
        # maintainer may have been busy while new PRs arrived, and waiting for
        # its old context to finish used to leave every other slot idle.
        retired_maintainers = self.retire_unusable_maintainers(conn, actor)
        planner_batches = self.replenish_planners(conn, actor)
        maintainer_batches = self.replenish_maintainers(conn, actor)
        orchestrator_episodes = self.replenish_orchestrators(conn, actor, now)
        from .coordination_memory import reconcile
        coordination_recoveries = reconcile(self, conn, actor, now)
        rechecks = self.recheck_idle_runs(conn, actor, now)
        return {"recovered": recovered, "expired": len(expired), "completed": completed,
                "idle_rechecks": rechecks,
                "coordination_recoveries": coordination_recoveries,
                "maintainer_batches": maintainer_batches, "planner_batches": planner_batches,
                "orchestrator_episodes": orchestrator_episodes,
                "retired_maintainers": retired_maintainers}

    def recheck_idle_runs(self, conn, actor, now):
        from .refill import recheck
        return recheck(self, conn, actor, now)

    def cancel(self, conn, actor, item, reason, *, disable_automation=True, reconsider=True):
        if item["status"] in ("completed", "failed", "cancelled"):
            return item
        now = conn.execute(select(func.now())).scalar_one()
        status = "stopping" if item["status"] in ("running", "stopping") else "cancelled"
        row = change(conn, "assignment", item["id"], status=status, status_note=reason,
                     finished_at=now if status == "cancelled" else None)
        execution = tables["execution"]
        conn.execute(update(execution).where(execution.c.assignment_id == item["id"],
            execution.c.status.in_(("starting", "running"))).values(status="stopping"))
        if item["automation_id"] and disable_automation:
            change(conn, "automation", item["automation_id"], enabled=False)
        if status == "cancelled" and reconsider:
            self.reconsider(conn, actor, row)
        emit(conn, actor.id, project_of(conn, "assignment", item["id"]), "assignment", row,
             ["status", "status_note"], previous=item["status"])
        return row
