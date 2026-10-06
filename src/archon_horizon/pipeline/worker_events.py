"""Provenance-checked worker replay; old epochs preserve evidence, never edit live work."""

from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

from pydantic import Field, StrictInt
from sqlalchemy import func, select, or_
from sqlalchemy.dialects.postgresql import insert

from .auth import require_host
from .errors import DomainError
from .intent_reconciliation import JOURNAL_BLOCKER_PREFIX, blocker_id, repair_command
from .models import Contract, Failure
from .records import change, create, emit, get, next_number, object_ref, project_of, save_blob
from .schema import tables
from .service import LIVE_EXECUTIONS


class WorkerOperation(Contract):
    operation_id: UUID
    execution_id: UUID
    epoch: StrictInt = Field(ge=1)
    kind: Literal["activity", "execution_finished", "publication_discovered", "publication_verified", "publication_failed", "provider_observed", "workspace_prepared"]
    payload: dict
    occurred_at: float = Field(gt=0, allow_inf_nan=False)


class UnresolvedIntent(Contract):
    id: str = Field(min_length=1, max_length=200)
    status: Literal['pending', 'rejected']


class IntentReconciliation(Contract):
    pending: StrictInt = Field(ge=0)
    fingerprint: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    consecutive_observations: StrictInt = Field(ge=0)
    intents: list[UnresolvedIntent] = Field(default_factory=list, max_length=20)


def reconcile_agent_journal(conn, execution, state, *, unresolved):
    """Only the owning host's journal observation can settle this recovery gate."""
    table = tables['obligation']
    assignment_id = execution['assignment_id']
    identifier = blocker_id(assignment_id)
    rows = list(conn.execute(select(table).where(table.c.assignment_id == assignment_id,
        table.c.created_by_execution_id.is_not(None),
        table.c.description.startswith(JOURNAL_BLOCKER_PREFIX))).mappings())
    current = next((row for row in rows if row['id'] == identifier), None)
    if unresolved:
        description = (JOURNAL_BLOCKER_PREFIX + ' with `horizon-pipeline agent pending`. '
            'Inspect the authoritative outcome before replaying an uncertain write. For each repaired, '
            'superseded or delegated intent, run its recovery_command: '
            '`horizon-pipeline agent resolve-intent INTENT_ID --note "Outcome, evidence, and corrected operation or owner"`. '
            'Resolving this server obligation alone does not clear the local journal. '
            'The host will settle this blocker when its next replay confirms no unresolved intents.')
        if state and state.intents:
            description += '\nUnresolved intents:\n' + '\n'.join(
                f'- {item.status}: {repair_command(item.id)}' for item in state.intents)
            if state.pending > len(state.intents):
                description += f'\n{state.pending - len(state.intents)} additional intents: inspect agent pending.'
        if current is None:
            current = create(conn, 'obligation', id=identifier, assignment_id=assignment_id,
                created_by_execution_id=execution['id'], number=next_number(conn, 'obligation', 'assignment_id', assignment_id),
                kind='blocker', description=description)
        elif current['status'] != 'open' or current['description'] != description:
            current = change(conn, 'obligation', identifier, status='open', resolution=None, description=description)
        for row in rows:
            if row['id'] != identifier and row['status'] == 'open':
                change(conn, 'obligation', row['id'], status='superseded', resolution={
                    'kind': 'superseded', 'note': 'The host tracks this journal in one stable recovery obligation.',
                    'replacement_obligation_ids': [str(identifier)]})
    elif state is not None and state.pending == 0:
        for row in rows:
            if row['status'] == 'open':
                change(conn, 'obligation', row['id'], status='done', resolution={
                    'kind': 'completed', 'note': 'The owning host replayed the local API journal and confirmed '
                    'that no pending or rejected intents remain.', 'evidence': []})


def _request_failure_activity(conn, actor, execution, request, failure, observed, project_id):
    activity = create(conn, "activity", assignment_id=execution["assignment_id"], execution_id=execution["id"],
        provider_thread_id=request["provider_thread_id"], provider_request_id=request["id"], kind="failure",
        summary=f"Provider request failed ({failure['kind']}/{failure['code']}): {failure['message'][:2000]}",
        occurred_at=observed)
    emit(conn, actor.id, project_id, "activity", activity, ["kind", "summary"], execution_id=execution["id"])


def handle(conn, actor, operation: WorkerOperation, service, scheduler):
    execution = get(conn, "execution", operation.execution_id, lock=True)
    require_host(conn, actor, execution["host_id"])
    if execution["number"] != operation.epoch:
        raise DomainError("stale_epoch", "Worker epoch does not match its recorded execution")
    project_id = project_of(conn, "execution", execution["id"])
    now = conn.execute(select(func.now())).scalar_one()
    live = execution["status"] in ("starting", "running") and execution["lease_expires_at"] > now
    payload = operation.payload
    observed = datetime.fromtimestamp(operation.occurred_at, timezone.utc)
    if operation.kind == "workspace_prepared":
        if not live:
            raise DomainError("stale_epoch", "A fenced execution cannot acknowledge workspace preparation")
        workspace = get(conn, "workspace", execution["workspace_id"], lock=True)
        host = get(conn, "host", execution["host_id"])
        expected_path = str(PurePosixPath(host["workspace_root"]) / "assignments" / str(execution["assignment_id"]))
        if (set(payload) != {"workspace_id", "head_commit_oid", "branch_name"}
                or payload["workspace_id"] != str(workspace["id"])
                or workspace["host_id"] != execution["host_id"] or workspace["path"] != expected_path
                or workspace["branch_name"] != "horizon/assignments/" + str(execution["assignment_id"])):
            raise DomainError("scope_mismatch", "Workspace preparation must identify this execution's allocated worktree", 422)
        if payload["head_commit_oid"] != workspace["base_commit_oid"] or payload["branch_name"] != workspace["branch_name"]:
            raise DomainError("workspace_snapshot_mismatch", "Prepared workspace differs from its pinned commit or branch", 422)
        if workspace["status"] not in ("preparing", "ready"):
            raise DomainError("workspace_unavailable", "Workspace preparation was retired or fenced")
        if workspace["status"] == "preparing":
            workspace = change(conn, "workspace", workspace["id"], status="ready", head_commit_oid=payload["head_commit_oid"])
            emit(conn, actor.id, project_id, "workspace", workspace, ["status", "head_commit_oid"], execution_id=execution["id"])
        return {"workspace_id": workspace["id"], "status": workspace["status"]}
    if operation.kind in ("publication_discovered", "publication_verified", "publication_failed"):
        workspace = get(conn, "workspace", execution["workspace_id"])
        if UUID(payload["repository_id"]) != workspace["repository_id"]:
            raise DomainError("scope_mismatch", "Worker can preserve only its assigned repository", 422)
        oid = payload.get("commit_oid", "")
        if not isinstance(oid, str) or not 40 <= len(oid) <= 128 or any(c not in "0123456789abcdef" for c in oid):
            raise DomainError("invalid_commit", "Commit identity must be a hexadecimal Git object ID", 422)
        artifact = tables["artifact"]
        content = {"repository_id": payload["repository_id"], "commit_oid": oid}
        row = conn.execute(select(artifact).where(artifact.c.project_id == project_id,
            artifact.c.kind == "commit", artifact.c.content == content)).mappings().first()
        if not row:
            row = create(conn, "artifact", project_id=project_id, created_by_execution_id=execution["id"],
                         kind="commit", content=content)
        conn.execute(insert(tables["assignment_artifact"]).values(assignment_id=execution["assignment_id"],
            artifact_id=row["id"]).on_conflict_do_nothing())
        # Preservation refs never authorize semantic changes to a protected branch.
        recovery_ref = f"refs/horizon/preserved/{oid}"
        target = {"kind": "git", "repository_id": payload["repository_id"],
                  "ref_name": recovery_ref, "expected_old_oid": None}
        publication = tables["publication"]
        preserved = conn.execute(select(publication).where(publication.c.artifact_id == row["id"],
            publication.c.target["repository_id"].astext == payload["repository_id"],
            or_(publication.c.target["ref_name"].astext.startswith("refs/horizon/"),
                publication.c.target["ref_name"].astext.startswith("refs/heads/horizon/recovery/")))
            .order_by(publication.c.created_at)).mappings().first()
        if not preserved:
            preserved = create(conn, "publication", artifact_id=row["id"],
                               requested_by_assignment_id=execution["assignment_id"], target=target)
        if operation.kind == "publication_verified" and preserved["status"] != "verified":
            remote_ref = payload.get("remote_ref", "")
            if not remote_ref.startswith(("refs/horizon/", "refs/heads/horizon/recovery/")):
                raise DomainError("invalid_preservation_ref", "Host verification must name a dedicated preservation ref", 422)
            # The actual verified immutable ref is the publication target.
            target["ref_name"] = remote_ref
            # First authoritative confirmation time; the immutable worker receipt
            # keeps a deterministic timestamp for replay after local compaction.
            preserved = change(conn, "publication", preserved["id"], status="verified", verified_at=now, target=target, failure=None)
        elif operation.kind == "publication_failed" and preserved["status"] != "verified":
            code = payload.get("failure_code", "publication_blocked")
            if not isinstance(code, str) or code not in {"publication_blocked", "git_authentication_failed", "git_remote_configuration", "git_recovery_ref_conflict",
                            "publication_remote_not_configured", "git_local_configuration", "local_object_missing"}:
                code = "publication_blocked"
            preserved = change(conn, "publication", preserved["id"], status="failed", failure={
                "kind": "configuration", "code": code,
                "message": "Worker publication needs repair: " + code + ". Inspect the worker publication journal."})
        emit(conn, actor.id, project_id, "publication", dict(preserved), ["status"], execution_id=execution["id"])
        return {"publication_id": preserved["id"], "status": preserved["status"]}
    if operation.kind == "execution_finished":
        status = payload.get("status")
        if status not in ("succeeded", "failed", "cancelled", "lost", "yielded"):
            raise DomainError("invalid_outcome", "Unknown execution outcome", 422)
        first_stop_receipt = execution["stop_confirmed_at"] is None
        if first_stop_receipt:
            execution = change(conn, "execution", execution["id"], stop_confirmed_at=now)
        # A daemon's stop receipt follows process-group/container cleanup, even after
        # central fencing. Timeout alone (the watchdog path) cannot settle requests.
        request_table = tables["provider_request"]
        from sqlalchemy import update
        conn.execute(update(request_table).where(request_table.c.execution_id == execution["id"],
            request_table.c.status.in_(("pending", "submitted", "running", "uncertain"))).values(
                status="interrupted", finished_at=observed))
        thread_table = tables["provider_thread"]
        primary = conn.execute(select(thread_table).where(thread_table.c.assignment_id == execution["assignment_id"],
            thread_table.c.kind == "primary", thread_table.c.status.in_(("creating", "available")))).mappings().first()
        if primary:
            native = payload.get("provider_thread_id") or primary["provider_thread_id"]
            if native and primary["provider_thread_id"] and native != primary["provider_thread_id"]:
                raise DomainError("context_identity_changed", "Stop receipt names a different native context", 422)
            if native:
                change(conn, "provider_thread", primary["id"], provider_thread_id=native, status="available")
            elif status != "succeeded" and conn.execute(select(request_table.c.id).where(
                    request_table.c.provider_thread_id == primary["id"]).limit(1)).first():
                # Admission is journaled before spawning the provider. A lost
                # acknowledgement or setup budget must not strand a context
                # with no evidence that the provider ever started.
                prelaunch_abort = (status == "yielded" and primary["status"] == "creating"
                    and payload.get("reason") in ("request_admission_not_acknowledged", "execution_budget_reached")
                    and not conn.execute(select(request_table.c.id).where(
                        request_table.c.provider_thread_id == primary["id"], or_(
                            request_table.c.execution_id != execution["id"],
                            request_table.c.started_at.is_not(None),
                            request_table.c.provider_turn_id.is_not(None),
                            request_table.c.status.in_(("completed", "failed")),
                        )).limit(1)).first())
                if not prelaunch_abort:
                    change(conn, "provider_thread", primary["id"], status="unavailable")
        failure = payload.get("failure")
        if failure:
            failure = Failure.model_validate(failure).model_dump(mode="json", exclude_none=True)
            # Older workers omitted the request diagnostic but retained the
            # provider failure in this stop receipt. Never infer child outcomes.
            if first_stop_receipt and status == "failed" and failure["kind"] in ("provider", "transport"):
                latest = conn.execute(select(request_table).join(thread_table,
                    request_table.c.provider_thread_id == thread_table.c.id).where(
                        request_table.c.execution_id == execution["id"], thread_table.c.kind == "primary")
                    .order_by(request_table.c.created_at.desc(), request_table.c.number.desc())
                    .limit(1)).mappings().first()
                if latest and latest["status"] == "failed" and latest["failure"] is None:
                    latest = change(conn, "provider_request", latest["id"], failure=failure)
                    emit(conn, actor.id, project_id, "provider_request", latest, ["failure"], execution_id=execution["id"])
                    _request_failure_activity(conn, actor, execution, latest, failure, observed, project_id)
        elif status in ("failed", "lost"):
            failure = {"kind": "host" if status == "lost" else "execution",
                       "code": "host_lost" if status == "lost" else "execution_failed",
                       "message": str(payload.get("reason", "Provider execution did not complete"))[:2000]}
        if not live and execution["status"] in ("starting", "running"):
            status = "lost"
            failure = {"kind": "host", "code": "lease_expired", "message": "Observed after execution lease expired"}
        claim = tables["resource_claim"]
        state = IntentReconciliation.model_validate(payload['intent_reconciliation']) if 'intent_reconciliation' in payload else None
        # The host snapshot is authoritative whenever it is present.  A
        # retained execution can carry the previous yield reason even after
        # replay has settled the journal; allowing that stale reason to win
        # would reopen the blocker and requeue an otherwise finished context.
        unresolved = (bool(state.pending) if state is not None
                      else payload.get('reason') == 'agent_intents_require_reconciliation')
        if live and (state is not None or unresolved):
            reconcile_agent_journal(conn, execution, state, unresolved=unresolved)
            assignment = get(conn, 'assignment', execution['assignment_id'])
            run = get(conn, 'run', assignment['run_id'])
            limit = run['retry_policy']['max_no_progress_requests']
            if unresolved and ((state and state.consecutive_observations >= limit)
                    or state is None and service.progress_stalled(conn, assignment['id'], limit)):
                status = 'failed'
                failure = {'kind': 'execution', 'code': 'agent_intent_reconciliation_stalled',
                    'message': 'The same local API intents remain unresolved across recovery executions. '
                    'Inspect agent pending, reconcile their authoritative outcomes, and use '
                    'agent resolve-intent INTENT_ID --note "Outcome and evidence" before resuming.'}
        result = scheduler.finish(conn, actor, execution, status, failure,
                                  journal_pending=unresolved, reason=payload.get("reason"))
        conn.execute(update(claim).where(claim.c.execution_id == execution["id"],
            claim.c.released_at.is_(None)).values(released_at=observed))
        return {"assignment_id": result["id"], "status": result["status"]}
    if operation.kind == "provider_observed":
        event = payload.get("event")
        thread_id = payload.get("provider_thread_record_id")
        if not thread_id:
            raise DomainError("missing_context", "Provider event needs its recorded context", 422)
        thread = get(conn, "provider_thread", thread_id)
        if thread["assignment_id"] != execution["assignment_id"]:
            raise DomainError("scope_mismatch", "Provider context belongs to another assignment", 422)
        if event == "request_started":
            if not live:
                raise DomainError("stale_epoch", "A fenced execution cannot submit a provider request")
            if get(conn, "workspace", execution["workspace_id"])["status"] != "ready":
                raise DomainError("workspace_not_ready", "Acknowledge verified workspace preparation before starting the provider")
            if thread["status"] not in ("creating", "available"):
                raise DomainError("context_unavailable", "Provider context is unavailable")
            prompt = payload.get("goal")
            if not isinstance(prompt, str) or not prompt.strip():
                raise DomainError("invalid_input", "Provider input must be recorded before submission", 422)
            revisions = {key: payload.get(key) for key in ("mission_revision_id", "mission_revision_number", "run_revision", "roadmap_snapshot_id")}
            if revisions["mission_revision_id"]:
                pinned = get(conn, "record_revision", revisions["mission_revision_id"])
                assignment = get(conn, "assignment", execution["assignment_id"])
                reference = get(conn, "object_reference", pinned["object_id"])
                if reference["kind"] != "mission" or reference["mission_id"] != assignment["mission_id"]:
                    raise DomainError("scope_mismatch", "Provider input pins another mission", 422)
            blob = save_blob(conn, service.store, project_id, {"prompt": prompt, **revisions}, execution["id"])
            from .reviewer_invocations import assignment_request_fields
            request_fields = {"reason": "continuation" if thread["provider_thread_id"] else "assignment"}
            request_fields.update(assignment_request_fields(conn, service,
                get(conn, "assignment", execution["assignment_id"])))
            request = create(conn, "provider_request", id=UUID(payload["request_id"]),
                provider_thread_id=thread["id"], execution_id=execution["id"],
                number=next_number(conn, "provider_request", "provider_thread_id", thread["id"]),
                **request_fields,
                input_artifact_id=blob["id"], status="submitted", submitted_at=observed)
            # Applied mission revision is advanced only after the request is actually acknowledged.
            emit(conn, actor.id, project_id, "provider_request", request, ["status"], execution_id=execution["id"])
            if request["reviewer_descriptor_id"]:
                from .review_labels import invocation_state
                principal = tables["principal"]
                reviewer_actor_id = conn.execute(select(principal.c.id).where(
                    principal.c.execution_id == execution["id"], principal.c.kind == "agent")).scalar_one()
                invocation_state(conn, service, reviewer_actor_id, request, "running", str(request["id"]) + ":started")
            return {"provider_request_id": request["id"]}
        if event == "request_completed":
            request = get(conn, "provider_request", payload["request_id"])
            if request["execution_id"] != execution["id"] or request["provider_thread_id"] != thread["id"]:
                raise DomainError("scope_mismatch", "Provider result does not belong to this execution", 422)
            if request["status"] in ("completed", "failed", "interrupted"):
                return {"provider_request_id": request["id"], "status": request["status"]}
            status = "completed" if payload.get("status") == "completed" else (
                "completed" if payload.get("status") == "succeeded" else "failed")
            failure = payload.get("failure") if status == "failed" else None
            if failure:
                failure = Failure.model_validate(failure).model_dump(mode="json", exclude_none=True)
            request = change(conn, "provider_request", request["id"], status=status, finished_at=observed, failure=failure)
            native = payload.get("provider_thread_id")
            if native and thread["provider_thread_id"] and native != thread["provider_thread_id"]:
                raise DomainError("context_identity_changed", "Native context changed without explicit recovery", 422)
            if native:
                change(conn, "provider_thread", thread["id"], provider_thread_id=native, status="available")
            if status == "completed" and request["input_artifact_id"]:
                import json
                blob = get(conn, "artifact", request["input_artifact_id"])
                consumed = json.loads(service.store.read(blob["content"]["sha256"]))
                mission_revision_id = consumed.get("mission_revision_id")
                if mission_revision_id:
                    incoming = get(conn, "record_revision", mission_revision_id)
                    previous = get(conn, "record_revision", thread["applied_mission_revision_id"]) if thread["applied_mission_revision_id"] else None
                    if previous is None or incoming["object_revision"] >= previous["object_revision"]:
                        change(conn, "provider_thread", thread["id"], applied_mission_revision_id=UUID(mission_revision_id))
                run_revision = consumed.get("run_revision")
                if run_revision is not None and run_revision >= (thread["applied_run_revision"] or 0):
                    change(conn, "provider_thread", thread["id"], applied_run_revision=run_revision,
                           applied_roadmap_snapshot_id=UUID(consumed["roadmap_snapshot_id"]) if consumed.get("roadmap_snapshot_id") else None)
                service.reconcile_goal_updates(conn, thread_id=thread["id"])
            emit(conn, actor.id, project_id, "provider_request", request, ["status", "failure"], execution_id=execution["id"])
            if failure:
                _request_failure_activity(conn, actor, execution, request, failure, observed, project_id)
            return {"provider_request_id": request["id"], "status": status}
        if event == "native_event":
            # A native stream event is the first authoritative indication that
            # the submitted provider turn actually started.  Reflect that in
            # the primary request as well as native child observations so the
            # dashboard and admission checks do not report a live turn as
            # indefinitely "submitted".  Never regress a terminal request or
            # accept an event from another execution/context.
            request = get(conn, "provider_request", payload["request_id"])
            if request["execution_id"] != execution["id"] or request["provider_thread_id"] != thread["id"]:
                raise DomainError("scope_mismatch", "Provider event does not belong to this execution context", 422)
            if request["status"] in ("pending", "submitted"):
                request = change(conn, "provider_request", request["id"], status="running",
                                 started_at=request["started_at"] or observed)
                emit(conn, actor.id, project_id, "provider_request", request, ["status", "started_at"],
                     execution_id=execution["id"])
            from .provider_events import project_observation
            from .activity_display import select_provider_event
            projected = project_observation(conn, actor, execution, thread, payload["request_id"], payload["adapter"],
                                            payload["raw"], operation.operation_id, observed, project_id)
            retained = select_provider_event(payload["adapter"], payload["raw"])
            if retained is not None:
                blob = save_blob(conn, service.store, project_id, retained, execution["id"])
                if projected.get("activity_id"):
                    conn.execute(insert(tables["activity_artifact"]).values(
                        activity_id=projected["activity_id"], artifact_id=blob["id"]).on_conflict_do_nothing())
                create(conn, "event", project_id=project_id, subject_id=object_ref(conn, "provider_thread", thread["id"]),
                       actor_principal_id=actor.id, execution_id=execution["id"], kind="provider_observed", schema_version=1,
                       source=f"host:{execution['host_id']}", source_event_id=str(operation.operation_id), occurred_at=observed,
                       payload={"provider_thread_id": str(thread["id"]), "provider_event_id": str(operation.operation_id),
                                "body_artifact_id": str(blob["id"])})
            return projected
        # Native provider details are immutable evidence; they are never trusted commands.
        blob = save_blob(conn, service.store, project_id, payload, execution["id"])
        row = create(conn, "event", project_id=project_id, subject_id=object_ref(conn, "provider_thread", thread["id"]),
                     actor_principal_id=actor.id, execution_id=execution["id"], kind="provider_observed", schema_version=1,
                     source=f"host:{execution['host_id']}", source_event_id=str(operation.operation_id), occurred_at=observed,
                     payload={"provider_thread_id": str(thread["id"]), "provider_event_id": str(operation.operation_id),
                              "body_artifact_id": str(blob["id"])})
        return {"event_id": row["id"]}
    kind = payload.get("kind", "progress")
    if kind not in ("checkpoint", "progress", "tool_use", "completion", "failure"):
        raise DomainError("invalid_activity", "Unknown activity kind", 422)
    summary = payload.get("summary")
    if summary is not None and (not isinstance(summary, str) or len(summary) > 16000):
        raise DomainError("invalid_activity", "Activity summary is too large", 422)
    from .activity_display import normalize_skills
    row = create(conn, "activity", assignment_id=execution["assignment_id"], execution_id=execution["id"],
                 kind=kind, summary=summary, skills_used=normalize_skills(payload.get("skills_used", [])),
                 occurred_at=observed)
    emit(conn, actor.id, project_id, "activity", row, ["kind", "summary", "skills_used"], execution_id=execution["id"])
    return {"activity_id": row["id"]}
