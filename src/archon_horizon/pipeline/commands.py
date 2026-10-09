"""Revisioned commands used by the dashboard and agents."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError
from sqlalchemy import func, select, update

from .auth import live_execution, require_admin, require_project
from .errors import DomainError
from .models import AssignmentCreate, Condition
from .persistence.records import change, create, emit, get, next_number, project_of, snapshot
from .persistence.schema import tables
from .missions.mission_tree import bump_parent, ensure_closure_allowed, require_assignment_authority, require_mission_authority


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str
    target_id: UUID
    expected_revision: StrictInt = Field(ge=1)
    args: dict = Field(default_factory=dict)


COMMAND_TARGETS = {
    "complete_mission": "mission", "cancel_mission": "mission", "reopen_mission": "mission",
    "pause_run": "run", "resume_run": "run", "cancel_run": "run", "drain_run": "run",
    "complete_run": "run", "reopen_run": "run", "adopt_roadmap_snapshot": "run", "queue_repair": "run",
    "extend_run_budget": "run", "set_run_budget": "run",
    "cancel_assignment": "assignment", "retry_assignment": "assignment", "move_before": "assignment",
    "move_after": "assignment", "update_assignment": "assignment", "defer_automation": "automation",
    "checkpoint_assignment": "assignment", "resume_assignment": "assignment",
    "retry_publication": "publication", "set_subscription": "discussion",
    "retry_delivery": "outbox_operation", "cancel_delivery": "outbox_operation",
    "reconcile_delivery_absent": "outbox_operation",
    "recover_context": "provider_thread", "edit_obligation": "obligation", "reopen_obligation": "obligation",
    "confirm_host_stopped": "execution",
    "request_review": "forge_item", "settle_review": "forge_item",
    "comment_obligation": "obligation", "resume_session": "assignment",
    "retire_workspace": "workspace",
    "accept_phase": "run", "request_maintenance": "run", "reset_circuit": "resource_limit",
}


def check_args(args: dict, allowed: set[str], required: set[str] = frozenset()) -> None:
    if set(args) - allowed or required - set(args):
        raise DomainError("invalid_arguments", "Command has missing or unsupported arguments", 422,
                          allowed=sorted(allowed), required=sorted(required))


def restore_recurring_assignment(conn, actor, assignment, project_id):
    """An explicit retry resumes its recurrence, but never duplicates a successor."""
    if not assignment["automation_id"]:
        return
    automation = get(conn, "automation", assignment["automation_id"], lock=True)
    active = tables["assignment"]
    if conn.execute(select(active.c.id).where(active.c.automation_id == automation["id"],
            active.c.id != assignment["id"], active.c.status.in_(("pending", "running", "stopping")))).first():
        raise DomainError("active_successor", "Settle the recurring successor before resuming its previous context")
    if not automation["enabled"]:
        resumed = change(conn, "automation", automation["id"], automation["revision"], enabled=True)
        snapshot(conn, "automation", resumed, actor.id)
        emit(conn, actor.id, project_id, "automation", resumed, ["enabled"],
             note=f"Explicit recovery of assignment {assignment['id']} re-enabled its recurring rule")


def execute(conn, actor, command: Command, service, scheduler):
    """Apply one authorized lifecycle decision at its expected record revision.

    The caller holds the mutation transaction lock and owns replay receipts.
    Command-specific handlers preserve physical-stop uncertainty, retained
    context and unresolved delivery ownership across logical status changes.
    """
    op, identifier, args = command.operation, command.target_id, command.args
    if op not in COMMAND_TARGETS:
        raise DomainError("unknown_command", "Unsupported command", 422)
    from .command_args import COMMAND_ARGS
    try:
        args = COMMAND_ARGS[op].model_validate(args).model_dump(mode="json", exclude_unset=True)
    except ValidationError as error:
        raise DomainError("invalid_arguments", "Command arguments do not match the contract", 422,
                          fields=[{"path": list(item["loc"]), "message": item["msg"]} for item in error.errors()]) from None
    if op in ("resume_session", "reset_circuit", "request_maintenance", "accept_phase", "settle_review") and not args["note"].strip():
        raise DomainError("decision_required", "Record the diagnosis or decision before continuing", 422)
    kind = COMMAND_TARGETS[op]
    old = get(conn, kind, identifier, lock=True)
    project_id = project_of(conn, kind, identifier)
    require_project(conn, actor, project_id, "maintainer" if kind == "run" and op != "request_maintenance" else "worker")
    if kind == "assignment":
        require_assignment_authority(conn, actor, old)
    elif kind == "automation" and actor.kind == "agent":
        if live_execution(conn, actor)["run_id"] != old["run_id"]:
            raise DomainError("forbidden", "The automation belongs to another run", 403)
        require_mission_authority(conn, actor, old["mission_id"])
    if old["revision"] != command.expected_revision:
        raise DomainError("revision_conflict", "The record changed; refresh and retry", current_revision=old["revision"])
    if op == "retire_workspace":
        from .operations.workspace_retention import retire
        return retire(conn, actor, old, service, args["note"])
    if op == "reset_circuit":
        require_admin(conn, actor)
        row = change(conn, kind, identifier, command.expected_revision, circuit_open=False,
                     circuit_reason=None, failure_count=0, cooldown_until=None)
        emit(conn, actor.id, project_id, None, row, ["circuit_open"], note=args["note"])
        return row
    if op == "request_maintenance":
        from .execution.objectives import request_maintenance
        return request_maintenance(scheduler, conn, actor, old, args)
    if op == "request_review":
        from .review.demand import request
        return request(conn, actor, service, identifier, args["note"], run_id=args.get("run_id"))
    elif op == "settle_review":
        from .review.demand import settle
        return settle(conn, actor, service, identifier, args["generation"], args["note"])
    elif op == "resume_session":
        require_project(conn, actor, project_id, "maintainer")
        if old["status"] != "pending" or not old.get("pause_reason"):
            raise DomainError("session_not_paused", "Resume an existing paused session; do not queue a replacement", 409)
        campaign = get(conn, "run", old["run_id"])
        if campaign["status"] != "active":
            raise DomainError("run_not_active", "Resume the objective before its session", 409)
        row = change(conn, kind, identifier, command.expected_revision, pause_reason=None, retry_at=None,
            not_before=None, status_note=args["note"])
        # Recovery history remains visible. A recorded repair grants a new
        # bounded attempt, rather than erasing previous failures.
        snapshot(conn, kind, row, actor.id)
    elif op == "confirm_host_stopped":
        require_admin(conn, actor)
        check_args(args, {"note", "evidence", "machine_fenced"}, {"note", "evidence", "machine_fenced"})
        if args["machine_fenced"] is not True or any(not isinstance(args[key], str) or not args[key].strip()
                or len(args[key]) > 16000 for key in ("note", "evidence")):
            raise DomainError("physical_fencing_required", "Confirm the physical host or process was fenced and record evidence", 422)
        if old["status"] not in ("lost", "cancelled") or old["stop_confirmed_at"] is not None:
            raise DomainError("invalid_stop_confirmation", "Only an unconfirmed lost execution can be reconciled")
        now = conn.execute(select(func.now())).scalar_one()
        row = change(conn, "execution", identifier, command.expected_revision, stop_confirmed_at=now)
        request, claim = tables["provider_request"], tables["resource_claim"]
        conn.execute(update(request).where(request.c.execution_id == identifier,
            request.c.status.in_(("pending", "submitted", "running", "uncertain"))).values(status="interrupted", finished_at=now))
        conn.execute(update(claim).where(claim.c.execution_id == identifier,
            claim.c.released_at.is_(None)).values(released_at=now))
        thread = tables["provider_thread"]
        conn.execute(update(thread).where(thread.c.assignment_id == old["assignment_id"], thread.c.kind == "primary",
            thread.c.provider_thread_id.is_(None), thread.c.status.in_(("creating", "available")),
            thread.c.id.in_(select(request.c.provider_thread_id).where(request.c.execution_id == identifier))).values(status="unavailable"))
        emit(conn, actor.id, project_id, "assignment", get(conn, "assignment", old["assignment_id"]),
             ["physical_stop_confirmed"], execution_id=identifier,
             note=f"Operator confirmed physical fencing: {args['note']}\nEvidence: {args['evidence']}")
        return row
    elif kind == "obligation":
        service.require_ledger_owner(conn, actor, old["assignment_id"])
        if op == "comment_obligation":
            comments = old["comments"]
            if len(comments) >= 128:
                raise DomainError("comment_limit", "Summarize further discussion in a linked workspace note", 409)
            row = change(conn, kind, identifier, command.expected_revision, comments=comments + [{
                "author_id": str(actor.id), "markdown": args["note"], "created_at": datetime.now(timezone.utc).isoformat()}])
        elif op == "edit_obligation":
            check_args(args, {"description", "kind"}, {"description"})
            if old["status"] != "open":
                raise DomainError("obligation_settled", "Reopen a settled obligation before editing it")
            from .models import ObligationCreate
            parsed = ObligationCreate(assignment_id=old["assignment_id"], description=args["description"],
                                      kind=args.get("kind", old["kind"]))
            row = change(conn, kind, identifier, command.expected_revision, description=parsed.description, kind=parsed.kind)
        else:
            check_args(args, {"note"}, {"note"})
            if not isinstance(args["note"], str) or not args["note"].strip():
                raise DomainError("missing_note", "Reopening an obligation requires an explanation", 422)
            row = change(conn, kind, identifier, command.expected_revision, status="open", resolution=None)
    elif kind == "outbox_operation":
        check_args(args, {"note"}, {"note"})
        if not isinstance(args["note"], str) or not args["note"].strip():
            raise DomainError("missing_recovery_note", "Delivery recovery needs an explanation", 422)
        if old["kind"] not in {"zulip_post", "forge_label", "forge_review", "forge_merge", "forge_create", "forge_comment", "forge_change", "forge_edit"}:
            raise DomainError("invalid_delivery", "This intent is managed by another lifecycle", 422)
        if op == "reconcile_delivery_absent":
            require_admin(conn, actor)
            if old["status"] != "uncertain":
                raise DomainError("delivery_not_uncertain", "Only an uncertain delivery can be reconciled as absent", 409)
            row = change(conn, kind, identifier, command.expected_revision, status="failed",
                retry_at=None, lease_owner=None, lease_expires_at=None,
                failure={"kind": "transport", "code": "remote_absence_confirmed", "message": args["note"]})
            emit(conn, actor.id, project_id, "project", get(conn, "project", project_id), ["deliveries"],
                 note=f"{op} {identifier}; original principal {old['actor_principal_id']}; "
                      f"uncertain -> failed without resending. Remote evidence: {args['note']}")
            from .execution.notifications import delivery_failed
            delivery_failed(conn, row)
            return row
        if old["status"] not in ("pending", "failed"):
            raise DomainError("delivery_unsettled", "An uncertain or running delivery must be reconciled before changing it")
        previous_actor = get(conn, "principal", old["actor_principal_id"])
        own_assignment = False
        if actor.kind == "agent" and previous_actor["execution_id"]:
            own_assignment = (live_execution(conn, actor)["assignment_id"] ==
                              get(conn, "execution", previous_actor["execution_id"])["assignment_id"])
        if not own_assignment or old["kind"] in ("forge_review", "forge_merge"):
            require_project(conn, actor, project_id, "maintainer")
        row = change(conn, kind, identifier, command.expected_revision,
            status="cancelled" if op == "cancel_delivery" else "pending", actor_principal_id=actor.id,
            retry_at=None, failure=None, lease_owner=None, lease_expires_at=None)
        emit(conn, actor.id, project_id, "project", get(conn, "project", project_id), ["deliveries"],
             note=f"{op} {identifier}; previous principal {old['actor_principal_id']}: {args['note']}")
        return row
    elif op == "recover_context":
        check_args(args, {"note"}, {"note"})
        require_project(conn, actor, project_id, "maintainer")
        if not isinstance(args["note"], str) or not args["note"].strip() or old["kind"] != "primary":
            raise DomainError("invalid_recovery", "Primary context recovery needs an explanation", 422)
        assignment = get(conn, "assignment", old["assignment_id"], lock=True)
        execution, request, thread = (tables[name] for name in ("execution", "provider_request", "provider_thread"))
        if get(conn, "run", assignment["run_id"]).get("orchestration") == "objective" and conn.execute(select(execution.c.id).where(
                execution.c.assignment_id == assignment["id"], execution.c.stop_confirmed_at.is_(None)).limit(1)).first():
            raise DomainError("physical_stop_unconfirmed", "Confirm the original process stopped before replacing an unusable native context", 409)
        if assignment["status"] not in ("pending", "failed") or conn.execute(select(execution.c.id).where(
                execution.c.assignment_id == assignment["id"], execution.c.status.in_(("starting", "running", "stopping")))).first():
            raise DomainError("execution_active", "Stop and reconcile the previous execution before replacing its context")
        if conn.execute(select(thread.c.id).where(thread.c.predecessor_id == identifier)).first():
            raise DomainError("context_replaced", "This context already has a replacement")
        if conn.execute(select(request.c.id).join(thread, request.c.provider_thread_id == thread.c.id).where(thread.c.assignment_id == assignment["id"],
                request.c.status.in_(("pending", "submitted", "running", "uncertain")))).first():
            raise DomainError("request_unsettled", "Reconcile outstanding requests before replacing their context")
        change(conn, kind, identifier, command.expected_revision, status="closed")
        replacement = create(conn, "provider_thread", assignment_id=assignment["id"], kind="primary",
            number=next_number(conn, "provider_thread", "assignment_id", assignment["id"]),
            workspace_id=old["workspace_id"], harness_revision_id=old["harness_revision_id"],
            skill_bundle_artifact_id=old["skill_bundle_artifact_id"],
            provider_state_ref=f"assignments/{assignment['id']}/recovery/{old['id']}",
            predecessor_id=old["id"], recovery_note=args["note"], applied_model_options=old["applied_model_options"])
        objective_mode = get(conn, "run", assignment["run_id"]).get("orchestration") == "objective"
        row = change(conn, "assignment", assignment["id"], status="pending", finished_at=None, retry_at=None,
                     **({"pause_reason": None} if objective_mode else {"recovery_attempts": 0}),
                     status_note="Provider context explicitly replaced; source and ledger retained")
        emit(conn, actor.id, project_id, "assignment", row, ["provider_thread"], note=args["note"])
        return replacement
    elif kind == "mission":
        check_args(args, {"note"}, {"note"})
        if not isinstance(args["note"], str) or not args["note"].strip():
            raise DomainError("invalid_decision", "Semantic mission decisions need an explanation", 422)
        require_mission_authority(conn, actor, identifier, role="worker")
        parent = get(conn, "mission", old["parent_id"]) if old["parent_id"] else None
        if op == "complete_mission":
            if old["status"] != "open":
                raise DomainError("mission_settled", "Only an open mission can be completed", 409)
            ensure_closure_allowed(conn, old)
            row = change(conn, kind, identifier, command.expected_revision, status="completed",
                         closed_at=func.now(), closure_note=args["note"])
        elif op == "cancel_mission":
            if old["status"] != "open":
                raise DomainError("mission_settled", "Only an open mission can be cancelled", 409)
            ensure_closure_allowed(conn, old, cancel=True)
            row = change(conn, kind, identifier, command.expected_revision, status="cancelled",
                         closed_at=func.now(), closure_note=args["note"])
        else:
            if old["status"] not in ("completed", "cancelled"):
                raise DomainError("mission_open", "Only a settled mission can be reopened", 409)
            if old["parent_id"]:
                if parent["status"] != "open":
                    raise DomainError("parent_closed", "Reopen the parent mission first", 409)
                open_siblings = conn.execute(select(func.count()).select_from(tables["mission"]).where(
                    tables["mission"].c.parent_id == parent["id"], tables["mission"].c.status == "open",
                    tables["mission"].c.id != identifier)).scalar_one()
                if open_siblings >= parent["max_open_children"]:
                    raise DomainError("child_budget_exhausted", "The mission child budget is exhausted", 409)
            row = change(conn, kind, identifier, command.expected_revision, status="open", closed_at=None, closure_note=None)
        snapshot(conn, kind, row, actor.id)
        if parent:
            bump_parent(conn, parent, actor.id)
    elif kind == "run":
        if op == "accept_phase":
            from .execution.objectives import accept_phase
            return accept_phase(scheduler, conn, actor, old, args)
        elif op == "set_run_budget":
            require_admin(conn, actor)
            if old["status"] not in ("active", "paused"):
                raise DomainError("invalid_transition", "Only active or paused runs can change their admission budget")
            row = change(conn, kind, identifier, command.expected_revision,
                         max_assignments=args["max_assignments"])
        elif op == "extend_run_budget":
            require_admin(conn, actor)
            if old["status"] not in ("active", "paused"):
                raise DomainError("invalid_transition", "Only active or paused runs can extend their admission budget")
            if old["max_assignments"] is None:
                raise DomainError("unlimited_run", "This run already has unlimited assignment admissions", 422)
            row = change(conn, kind, identifier, command.expected_revision,
                         max_assignments=old["max_assignments"] + args["additional_assignments"])
        elif op == "queue_repair":
            check_args(args, {"assignment", "note"}, {"assignment", "note"})
            if old["status"] != "draining" or not isinstance(args["note"], str) or not args["note"].strip():
                raise DomainError("invalid_repair", "Repair admission requires a draining run and an explanation", 422)
            data = AssignmentCreate.model_validate(args["assignment"])
            if data.run_id != identifier:
                raise DomainError("scope_mismatch", "Repair must belong to this run", 422)
            row = service.assignment(conn, actor, data, repair=True)
            emit(conn, actor.id, project_id, "assignment", row, ["repair_admission"], note=args["note"])
            return row
        elif op == "adopt_roadmap_snapshot":
            check_args(args, {"snapshot_id"}, {"snapshot_id"})
            from .persistence.records import same_project
            baseline = same_project(conn, "roadmap_snapshot", args["snapshot_id"], project_id)
            from .projects.milestones import require_baseline
            require_baseline(conn, baseline)
            if old["phase"]["kind"] != "formalization":
                raise DomainError("invalid_baseline", "This run does not consume a roadmap baseline")
            if old["adopted_roadmap_snapshot_id"]:
                objective_id = get(conn, "roadmap_snapshot", old["adopted_roadmap_snapshot_id"])["roadmap_document_id"]
            else:
                objective_id = old.get("objective_id") or get(conn, "mission", old["mission_id"])["roadmap_document_id"]
            if baseline["roadmap_document_id"] != objective_id:
                raise DomainError("invalid_baseline", "Adoption must keep the run's roadmap document", 422)
            row = change(conn, kind, identifier, command.expected_revision, adopted_roadmap_snapshot_id=baseline["id"])
            assignment = tables["assignment"]
            missions = conn.execute(select(assignment.c.mission_id).where(assignment.c.run_id == identifier,
                assignment.c.status.in_(("pending", "running"))).distinct()).scalars()
            for mission_id in missions:
                mission = get(conn, "mission", mission_id)
                service.goal_updates(conn, actor, mission_id, snapshot(conn, "mission", mission, actor.id))
        else:
            check_args(args, {"note"})
            targets = {"pause_run": ("active", "paused"), "resume_run": ("paused", "active"),
                       "drain_run": ("active", "draining"), "complete_run": ("draining", "completed"),
                       "reopen_run": (("draining", "completed"), "active")}
            if op == "cancel_run":
                if old["status"] in ("cancelled", "completed"):
                    raise DomainError("run_settled", "Run is already settled")
                status = "stopping"
            else:
                source, status = targets[op]
                if old["status"] not in ((source,) if isinstance(source, str) else source):
                    raise DomainError("invalid_transition", "Run cannot make this status transition")
            if op == "resume_run" and old.get("pending_phase") and not old.get("auto_advance") and actor.kind == "agent":
                raise DomainError("human_approval_required", "This objective is configured to pause for human phase approval", 403)
            if op in ("drain_run", "complete_run"):
                if get(conn, "mission", old["mission_id"])["status"] != "completed":
                    raise DomainError("mission_open", "Record the semantic mission-completion decision first")
            if op == "complete_run":
                assignment, publication = tables["assignment"], tables["publication"]
                pending = conn.execute(select(func.count()).select_from(assignment).where(
                    assignment.c.run_id == identifier, assignment.c.status.in_(("pending", "running", "stopping")))).scalar_one()
                unpublished = conn.execute(select(func.count()).select_from(publication.join(assignment,
                    publication.c.requested_by_assignment_id == assignment.c.id)).where(assignment.c.run_id == identifier,
                    publication.c.status.in_(("pending", "running", "failed")))).scalar_one()
                obligation = tables["obligation"]
                blockers = conn.execute(select(func.count()).select_from(obligation.join(assignment)).where(
                    assignment.c.run_id == identifier, obligation.c.status == "open",
                    (assignment.c.automation_id.is_(None) | assignment.c.started_at.is_not(None)))).scalar_one()
                execution, principal, outbox = (tables[name] for name in ("execution", "principal", "outbox_operation"))
                unconfirmed = conn.execute(select(execution.c.id).join(assignment).where(
                    assignment.c.run_id == identifier, execution.c.stop_confirmed_at.is_(None)).limit(1)).first()
                delivery = conn.execute(select(outbox.c.id).select_from(outbox.join(principal,
                    outbox.c.actor_principal_id == principal.c.id).join(execution,
                    principal.c.execution_id == execution.c.id).join(assignment,
                    execution.c.assignment_id == assignment.c.id)).where(assignment.c.run_id == identifier,
                    outbox.c.kind.in_(("zulip_post", "forge_label", "forge_review", "forge_merge", "forge_create", "forge_comment", "forge_change", "forge_edit")),
                    outbox.c.status.in_(("pending", "running", "uncertain", "failed"))).limit(1)).first()
                if pending or unpublished or blockers or unconfirmed or delivery:
                    raise DomainError("run_unsettled", "Work, physical execution stops, publication and external deliveries must settle before run completion")
            row = change(conn, kind, identifier, command.expected_revision, status=status,
                         status_note=args.get("note"), finished_at=func.now() if status == "completed" else None)
            if op == "reopen_run":
                mission = change(conn, "mission", old["mission_id"], status="open", closed_at=None, closure_note=None)
                snapshot(conn, "mission", mission, actor.id)
                automation = tables["automation"]
                for template in conn.execute(select(automation).where(automation.c.run_id == identifier,
                        automation.c.enabled.is_(True))).mappings():
                    if old.get("orchestration") == "objective":
                        from .execution.objectives import ensure_successor
                        ensure_successor(scheduler, conn, row, dict(template))
                    else:
                        scheduler.replenish(conn, actor, dict(template))
            if status in ("stopping", "draining"):
                automation, assignment = tables["automation"], tables["assignment"]
                if status == "stopping":
                    conn.execute(update(automation).where(automation.c.run_id == identifier).values(enabled=False))
                query = select(assignment).where(assignment.c.run_id == identifier,
                    assignment.c.status.in_(("pending", "running")))
                if status == "draining":
                    query = query.where(assignment.c.status == "pending", assignment.c.automation_id.is_not(None))
                for item in list(conn.execute(query).mappings()):
                    scheduler.cancel(conn, actor, dict(item), f"Run is {status}",
                                     disable_automation=status != "draining", reconsider=status != "draining")
    elif op == "cancel_assignment":
        check_args(args, {"note"})
        return scheduler.cancel(conn, actor, old, args.get("note", "Cancelled by an authorized user"))
    elif op == "retry_assignment":
        check_args(args, set())
        if old["status"] != "failed":
            raise DomainError("invalid_transition", "Only a failed assignment can be retried")
        if get(conn, "run", old["run_id"])["status"] not in ("active", "paused"):
            raise DomainError("run_not_active", "Resume the run before recovering this context")
        run = get(conn, "run", old["run_id"])
        if run.get("orchestration") == "objective":
            raise DomainError("session_preserved", "Use resume_session with a diagnosis; retry accounting is persistent", 409)
        restore_recurring_assignment(conn, actor, old, project_id)
        row = change(conn, kind, identifier, command.expected_revision, status="pending", finished_at=None,
                     retry_at=None, recovery_attempts=0, status_note="Explicit recovery requested")
    elif op in ("move_before", "move_after"):
        check_args(args, {"other_id"}, {"other_id"})
        if old["status"] != "pending":
            raise DomainError("not_pending", "Only pending assignments can move in the queue")
        target = get(conn, kind, args["other_id"])
        if target["run_id"] != old["run_id"] or target["status"] != "pending" or target["id"] == identifier:
            raise DomainError("invalid_neighbor", "Queue neighbor must be another pending assignment in the same run", 422)
        require_assignment_authority(conn, actor, target)
        table = tables[kind]
        rows = list(conn.execute(select(table).where(table.c.run_id == old["run_id"], table.c.status == "pending")
            .order_by(table.c.queue_rank, table.c.id)).mappings())
        order = [row["id"] for row in rows if row["id"] != identifier]
        index = order.index(target["id"]) + (1 if op == "move_after" else 0)
        order.insert(index, identifier)
        for index, item_id in enumerate(order):
            change(conn, kind, item_id, queue_rank=(index + 1) * 1024)
        row = get(conn, kind, identifier)
    elif op == "resume_assignment":
        if get(conn, "run", old["run_id"]).get("orchestration") == "objective":
            raise DomainError("session_settled", "Completed sessions stay settled; use a distinct bounded mission for new work or resume_session for suspended work", 409)
        require_admin(conn, actor)
        if old["status"] not in ("completed", "failed"):
            raise DomainError("invalid_transition", "Only a settled assignment can be explicitly resumed")
        if get(conn, "run", old["run_id"])["status"] not in ("active", "paused"):
            raise DomainError("run_not_active", "Resume the run before recovering this context")
        thread, owner = tables["provider_thread"], tables["assignment"]
        retained = conn.execute(select(thread).where(thread.c.assignment_id == identifier,
            thread.c.kind == "primary", thread.c.status.in_(("creating", "available")))).mappings().first()
        if retained and conn.execute(select(owner.c.id).join(thread, thread.c.assignment_id == owner.c.id).where(
                thread.c.workspace_id == retained["workspace_id"], thread.c.kind == "primary",
                thread.c.status.in_(("creating", "available")), owner.c.id != identifier,
                owner.c.status.in_(("pending", "running", "stopping"))).limit(1)).first():
            raise DomainError("workspace_reassigned", "This retained workspace is owned by another unfinished session; "
                "relocate the retained context to a verified independent workspace before resuming")
        restore_recurring_assignment(conn, actor, old, project_id)
        row = change(conn, kind, identifier, command.expected_revision, status="pending", finished_at=None,
                     not_before=None, start_condition=None, checkpoint_requested_at=None, retry_at=None,
                     status_note=args["note"])
        snapshot(conn, kind, row, actor.id)
    elif op == "checkpoint_assignment":
        if old["status"] != "running":
            raise DomainError("not_running", "Only a running assignment can request a checkpoint")
        if actor.kind == "agent" and live_execution(conn, actor)["assignment_id"] != identifier:
            raise DomainError("forbidden", "Agents may checkpoint only their own assignment", 403)
        condition = Condition.model_validate(args["start_condition"]).model_dump(mode="json") if args.get("start_condition") else None
        service.validate_condition(conn, project_id, condition, identifier)
        parsed = AssignmentCreate.model_validate({**{key: old[key] for key in AssignmentCreate.model_fields},
            "not_before": args.get("not_before"), "start_condition": condition})
        row = change(conn, kind, identifier, command.expected_revision, checkpoint_requested_at=func.now(),
                     not_before=parsed.not_before, start_condition=condition, status_note=args["note"])
        snapshot(conn, kind, row, actor.id)
    elif op == "update_assignment":
        check_args(args, {"not_before", "expires_at", "start_condition", "instructions", "functions"})
        if old["status"] != "pending":
            raise DomainError("not_pending", "Edit scheduling at a checkpoint while the assignment is pending")
        values = {key: old[key] for key in AssignmentCreate.model_fields}
        values.update(args)
        parsed = AssignmentCreate.model_validate(values)
        from .missions.service import FUNCTIONS
        if set(parsed.functions) - FUNCTIONS:
            raise DomainError("unknown_function", "Functions must exist in the pinned catalog", 422)
        condition = parsed.start_condition.model_dump(mode="json") if parsed.start_condition else None
        service.validate_condition(conn, project_id, condition, identifier)
        values = parsed.model_dump(include=set(args))
        if "start_condition" in args:
            values["start_condition"] = condition
        row = change(conn, kind, identifier, command.expected_revision, **values)
        snapshot(conn, kind, row, actor.id)
    elif op == "defer_automation":
        check_args(args, {"not_before", "start_condition", "cooldown_seconds", "enabled", "no_progress"})
        if "no_progress" in args and not isinstance(args["no_progress"], bool):
            raise DomainError("invalid_arguments", "no_progress must be a boolean", 422)
        from .models import AutomationCreate
        values = {key: old[key] for key in AutomationCreate.model_fields}
        values.update({key: value for key, value in args.items() if key in values})
        parsed = AutomationCreate.model_validate(values)
        condition = parsed.start_condition.model_dump(mode="json") if parsed.start_condition else None
        service.validate_condition(conn, project_id, condition)
        if "start_condition" in args:
            pending_table = tables["assignment"]
            pending_ids = conn.execute(select(pending_table.c.id).where(pending_table.c.automation_id == identifier,
                pending_table.c.status == "pending")).scalars()
            for pending_id in pending_ids:
                service.validate_condition(conn, project_id, condition, pending_id)
        updates = {key: value for key, value in parsed.model_dump().items() if key in args}
        if "start_condition" in updates:
            updates["start_condition"] = condition
        if "enabled" in args:
            if not isinstance(args["enabled"], bool):
                raise DomainError("invalid_arguments", "enabled must be a boolean", 422)
            updates["enabled"] = args["enabled"]
            if get(conn, "run", old["run_id"]).get("orchestration") == "objective":
                updates["pause_reason"] = None if args["enabled"] else "operator"
        if args.get("no_progress"):
            count = old["no_progress_count"] + 1
            fallback = datetime.now(timezone.utc) + timedelta(seconds=min(3600, old["cooldown_seconds"] * 2 ** min(count, 8)))
            updates.update(no_progress_count=count, not_before=max(parsed.not_before, fallback) if parsed.not_before else fallback)
        elif args.get("no_progress") is False:
            updates["no_progress_count"] = 0
        row = change(conn, kind, identifier, command.expected_revision, **updates)
        snapshot(conn, kind, row, actor.id)
        scheduling = {key: row[key] for key in ("start_condition", "not_before") if key in updates}
        if scheduling:
            assignment = tables["assignment"]
            for pending in conn.execute(select(assignment).where(assignment.c.automation_id == identifier,
                    assignment.c.status == "pending")).mappings():
                change(conn, "assignment", pending["id"], **scheduling)
        # Enabling a dormant profile is an explicit orchestration decision.
        # Admit its single recurrence immediately; the assignment's condition
        # still controls when a host may claim it.  The scheduler's duplicate
        # guard makes retries of the same command idempotent.
        if args.get("enabled") is True and not old["enabled"]:
            campaign = get(conn, "run", row["run_id"])
            if campaign.get("orchestration") == "objective":
                from .execution.objectives import ensure_successor
                pending = ensure_successor(scheduler, conn, campaign, row)
                if pending:
                    change(conn, "assignment", pending["id"], pause_reason=None)
            else:
                scheduler.replenish(conn, actor, row)
        elif args.get("enabled") is False and get(conn, "run", old["run_id"]).get("orchestration") == "objective":
            conn.execute(update(tables["assignment"]).where(tables["assignment"].c.automation_id == identifier,
                tables["assignment"].c.status == "pending").values(pause_reason="Planner automation paused explicitly"))
    elif op == "retry_publication":
        check_args(args, set())
        if old["status"] != "failed":
            raise DomainError("invalid_transition", "Only failed publication can be retried")
        if old["target"].get("ref_name", "").startswith(("refs/horizon/", "refs/heads/horizon/recovery/")):
            raise DomainError("worker_repair_required", "Repair and retry this publication on its worker with horizon-pipeline worker-publications", 409)
        row = change(conn, kind, identifier, command.expected_revision, status="pending", retry_at=None, failure=None)
    elif op == "set_subscription":
        check_args(args, {"assignment_id", "subscribed"}, {"assignment_id", "subscribed"})
        if not isinstance(args["subscribed"], bool):
            raise DomainError("invalid_arguments", "subscribed must be a boolean", 422)
        from .integrations.communications import subscribe
        from .models import SubscriptionCreate
        return subscribe(conn, actor, SubscriptionCreate(assignment_id=args["assignment_id"],
            subject={"kind": "discussion", "id": identifier}, mode="digest" if args["subscribed"] else "muted"), service)
    emit(conn, actor.id, project_id, kind, row, list(args) or ["status"],
         previous=old.get("status") if row.get("status") != old.get("status") else None, note=args.get("note"))
    return row
