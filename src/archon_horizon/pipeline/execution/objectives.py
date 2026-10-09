"""Objective ownership and one bounded planner recurrence, independent of providers.

Mutations run under the existing transaction lock. Database recurrence keys are
the final guard against duplicate successors; no provider call happens here.
"""

from datetime import timedelta
import hashlib
import json
import random
from uuid import UUID

from sqlalchemy import Text, cast, func, or_, select, update

from .. import models
from ..auth import require_project
from ..errors import DomainError
from ..instructions.templates import template
from ..persistence.records import change, create, emit, get, project_of, snapshot
from ..persistence.schema import tables

PLANNER = "objective-planner"
LIVE = ("pending", "running", "stopping")


def launch(scheduler, conn, actor, data):
    """Resolve an objective-only launch to existing repository/lease contracts."""
    objective_id = data.objective_id
    if objective_id is None and data.mission_id:
        objective_id = get(conn, "mission", data.mission_id)["roadmap_document_id"]
    if objective_id is None and data.phase and data.phase.kind == "preprocessing":
        objective_id = data.phase.roadmap_document_id
    if objective_id is None:
        raise DomainError("objective_required", "Select the versioned roadmap objective", 422)
    document = get(conn, "document", objective_id)
    require_project(conn, actor, document["project_id"], "maintainer")
    if document["kind"] != "roadmap":
        raise DomainError("invalid_objective", "An objective is a versioned roadmap Markdown document", 422)
    run = tables["run"]
    existing = conn.execute(select(run.c.id).where(run.c.objective_id == objective_id,
        run.c.orchestration == "objective", run.c.status.in_(("active", "paused", "draining", "stopping")))).scalar_one_or_none()
    if existing:
        raise DomainError("objective_already_active", "Resume the existing objective instead of launching duplicate owners", 409, run_id=str(existing))
    mission_table = tables["mission"]
    legacy = conn.execute(select(run.c.id).join(mission_table, mission_table.c.id == run.c.mission_id).where(
        run.c.orchestration == "legacy", run.c.status.in_(("active", "paused", "draining", "stopping")),
        or_(mission_table.c.roadmap_document_id == objective_id,
            run.c.phase["roadmap_document_id"].astext == str(objective_id),
            run.c.adopted_roadmap_snapshot_id.in_(select(tables["roadmap_snapshot"].c.id).where(
                tables["roadmap_snapshot"].c.roadmap_document_id == objective_id)))).limit(1)).first()
    if legacy:
        raise DomainError("legacy_objective_active", "Settle the existing legacy objective before switching orchestration", 409)
    mission_id = data.mission_id
    if mission_id is None:
        mission = scheduler.service.mission(conn, actor, models.MissionCreate(
            project_id=document["project_id"], roadmap_document_id=objective_id,
            title=document["title"], objective=template("objective/root-mission"),
            acceptance_criteria=[template("objective/root-acceptance")], max_open_children=128))
        mission_id = mission["id"]
    from ..persistence.records import same_project
    root = same_project(conn, "mission", mission_id, document["project_id"])
    if root["roadmap_document_id"] and root["roadmap_document_id"] != objective_id:
        raise DomainError("objective_mismatch", "The mission belongs to another objective", 422)
    if data.phase and data.phase.kind == "formalization" and data.phase.roadmap_snapshot_id:
        baseline = same_project(conn, "roadmap_snapshot", data.phase.roadmap_snapshot_id, document["project_id"])
        if baseline["roadmap_document_id"] != objective_id:
            raise DomainError("objective_mismatch", "The baseline belongs to another objective", 422)
    hosts = data.host_ids
    if not hosts:
        host = tables["host"]
        hosts = list(conn.execute(select(host.c.id).where(host.c.mode == "enabled").order_by(host.c.id)).scalars())
    # A default provider account guard gives even a small installation a shared
    # outage circuit without capping native children at primary host slots.
    # Explicit operator account quotas always take precedence.
    hh, link, limit = (tables[n] for n in ("host_harness", "host_harness_limit", "resource_limit"))
    for slot in list(conn.execute(select(hh).where(hh.c.host_id.in_(hosts), hh.c.enabled.is_(True))).mappings()):
        linked = conn.execute(select(limit.c.id).join(link).where(link.c.host_id == slot["host_id"],
            link.c.harness_id == slot["harness_id"], limit.c.kind == "provider_account")).first()
        if linked:
            continue
        slug = "objective-" + hashlib.sha256(slot["credential_ref"].encode()).hexdigest()[:24]
        account = conn.execute(select(limit).where(limit.c.kind == "provider_account", limit.c.slug == slug)).mappings().first()
        if account is None:
            account = create(conn, "resource_limit", kind="provider_account", slug=slug, max_concurrent=None)
        conn.execute(link.insert().values(host_id=slot["host_id"], harness_id=slot["harness_id"], resource_limit_id=account["id"]))
    phase = data.phase or models.Preprocessing(kind="preprocessing", roadmap_document_id=objective_id)
    if phase.kind == "preprocessing" and phase.roadmap_document_id != objective_id:
        raise DomainError("objective_mismatch", "The phase and launch must identify the same objective", 422)
    phases = data.requested_phases
    if not phases:
        phases = [phase.kind]
        if data.phase is None:
            phases.append("formalization")
            repo = tables["repository"]
            if conn.execute(select(repo.c.id).where(repo.c.project_id == document["project_id"], repo.c.purpose == "library").limit(1)).first():
                phases.append("postprocessing")
    if phases[0] != phase.kind:
        raise DomainError("phase_order", "requested_phases must start with the launched phase", 422)
    prepared = data.model_copy(update={"mission_id": mission_id, "objective_id": objective_id,
        "host_ids": hosts, "phase": phase, "requested_phases": phases})
    # Future objectives get discretionary specialists. Existing policy rows and
    # pinned sessions keep their configured review requirements.
    repo = tables["repository"]
    for target in conn.execute(select(repo).where(repo.c.project_id == document["project_id"],
            repo.c.purpose.in_(("knowledge", "library")), repo.c.archived_at.is_(None))).mappings():
        for kind in phases:
            if scheduler.matching_policy(conn, target["id"], kind):
                continue
            policy = create(conn, "review_policy", project_id=document["project_id"],
                slug=f"objective-{str(objective_id)[:8]}-{kind}-{str(target['id'])[:8]}", phases=[kind],
                instructions=template("objective/review-policy"), specialist_mode="advisory")
            conn.execute(tables["review_policy_repository"].insert().values(review_policy_id=policy["id"], repository_id=target["id"]))
            snapshot(conn, "review_policy", policy, actor.id)
    result = scheduler.run(conn, actor, prepared, _prepared=True)
    automation = create(conn, "automation", run_id=result["id"], name=PLANNER, mission_id=mission_id,
        role="worker", functions=["planner"], cooldown_seconds=scheduler.config.planner_min_interval_seconds,
        frontier_hash=frontier(conn, result))
    snapshot(conn, "automation", automation, actor.id)
    ensure_successor(scheduler, conn, result, automation)
    return get(conn, "run", result["id"])


def frontier(conn, run):
    """Hash meaningful inputs, excluding poll times and the planner's own history."""
    assignment, item = tables["assignment"], tables["forge_item"]
    # SQL aggregates keep dashboard/planner input bounded even after years of
    # sessions. Hash only meaningful fields; polling timestamps do not wake it.
    workers = list(conn.execute(select(assignment.c.status, func.count(),
        func.sum(func.hashtextextended(func.concat(cast(assignment.c.id, Text), assignment.c.status,
            func.coalesce(assignment.c.pause_reason, "")), 0))).where(
        assignment.c.run_id == run["id"], ~assignment.c.functions.contains(["planner"]))
        .group_by(assignment.c.status).order_by(assignment.c.status)).tuples())
    items = list(conn.execute(select(item.c.status, func.count(), func.sum(func.hashtextextended(
        func.concat(cast(item.c.id, Text), item.c.status, func.coalesce(item.c.head_commit_oid, ""),
            cast(item.c.labels, Text)), 0))).where(item.c.origin_run_id == run["id"])
        .group_by(item.c.status).order_by(item.c.status)).tuples())
    doc = get(conn, "document", run["objective_id"])
    health = list(conn.execute(select(tables["host"].c.id, tables["host"].c.mode, tables["host"].c.health).join(
        tables["run_host"], tables["run_host"].c.host_id == tables["host"].c.id).where(
        tables["run_host"].c.run_id == run["id"])).tuples())
    # Do not hash heartbeat timestamps or fluctuating free-byte counters. Only
    # admission state changes can justify waking an idle planning model.
    hosts = [(str(h), mode, (value or {}).get("status")) for h, mode, value in health]
    payload = [doc["source_commit_oid"], run["phase"], workers, items, hosts]
    return hashlib.sha256(json.dumps(payload, default=str, sort_keys=True).encode()).hexdigest()


def child_mission(scheduler, conn, run, title, objective, criteria):
    parent = get(conn, "mission", run["mission_id"])
    return scheduler.service.mission(conn, scheduler.system_actor(conn), models.MissionCreate(
        project_id=parent["project_id"], parent_id=parent["id"], expected_parent_revision=parent["revision"],
        roadmap_document_id=run["objective_id"], title=title, objective=objective,
        acceptance_criteria=criteria, delegation_note=template("objective/delegation")))


def ensure_successor(scheduler, conn, run, automation):
    if run["status"] != "active":
        return None
    key = f"planner:{run['id']}:next"
    assignment = tables["assignment"]
    found = conn.execute(select(assignment).where(assignment.c.recurrence_key == key,
        assignment.c.status.in_(LIVE))).mappings().first()
    if found:
        return dict(found)
    # Admission backpressure comes before creating a child mission, so failed
    # queue insertions cannot leave thousands of orphan planning missions.
    from .queue_policies import validate_enqueue
    data = models.AssignmentCreate(run_id=run["id"], mission_id=run["mission_id"], functions=["planner"], category="work",
        start_condition=models.Condition.model_validate(automation["start_condition"]) if automation["start_condition"] else None)
    try:
        validate_enqueue(conn, run, data)
    except DomainError as error:
        if error.code == "queue_full":
            return None
        raise
    mission = child_mission(scheduler, conn, run, "Plan the next useful work",
        template("objective/planning-mission"),
        [template("objective/planning-acceptance")])
    row = scheduler.service.assignment(conn, scheduler.system_actor(conn), data.model_copy(update={
        "mission_id": mission["id"], "instructions": template("objective/planning-instructions"),
        "not_before": automation["not_before"]}), automation_id=automation["id"], internal=True)
    row = change(conn, "assignment", row["id"], recurrence_key=key,
        pause_reason=None if automation["enabled"] else "Planner automation paused: " + (automation.get("pause_reason") or "idle"))
    return row


def admitted(scheduler, conn, item, run, now):
    if not item["automation_id"] or get(conn, "automation", item["automation_id"])["name"] != PLANNER:
        return item
    # The unique active key covers interrupted planners as well as live leases.
    row = change(conn, "assignment", item["id"], recurrence_key=f"planner:{run['id']}:active")
    automation = get(conn, "automation", item["automation_id"])
    ensure_successor(scheduler, conn, run, automation)
    return row


def completed(scheduler, conn, item, run, now):
    # Maintenance sessions close their bounded review mission after accounting
    # for their assigned generation. The objective root remains the integrator.
    if item.get("recurrence_key", "") and item["recurrence_key"].startswith(("review:", "maintenance:")):
        from ..missions.mission_tree import ensure_closure_allowed
        mission = get(conn, "mission", item["mission_id"])
        ensure_closure_allowed(conn, mission)
        change(conn, "mission", mission["id"], status="completed", closed_at=now, closure_note="Review generation settled; unresolved work remains owned by the objective.")
        return
    if not item["automation_id"] or get(conn, "automation", item["automation_id"])["name"] != PLANNER:
        return
    automation = get(conn, "automation", item["automation_id"])
    observed = frontier(conn, run)
    unchanged = automation["no_progress_count"] + 1 if observed == automation["frontier_hash"] else 0
    delay = min(scheduler.config.planner_max_idle_seconds,
        scheduler.config.planner_min_interval_seconds * 2 ** min(unchanged, 16))
    manual_pause = automation.get("pause_reason") not in (None, "idle")
    enabled = False if manual_pause else unchanged < scheduler.config.planner_max_unchanged_passes
    pause_reason = automation["pause_reason"] if manual_pause else None if enabled else "idle"
    automation = change(conn, "automation", automation["id"], frontier_hash=observed,
        no_progress_count=unchanged, enabled=enabled, pause_reason=pause_reason, not_before=now + timedelta(seconds=delay))
    successor = ensure_successor(scheduler, conn, run, automation)
    if successor:
        change(conn, "assignment", successor["id"], not_before=automation["not_before"],
            pause_reason=None if enabled else "Planner automation paused: " + (pause_reason or "idle"))
    # A bounded planning mission may close only if it did not retain children.
    mission = get(conn, "mission", item["mission_id"])
    child = tables["mission"]
    if not conn.execute(select(child.c.id).where(child.c.parent_id == mission["id"], child.c.status == "open").limit(1)).first():
        change(conn, "mission", mission["id"], status="completed", closed_at=now, closure_note="Bounded planning pass accounted for; objective remains open.")


def reconcile(scheduler, conn, actor, now):
    automation, run_table = tables["automation"], tables["run"]
    rules = conn.execute(select(automation).join(run_table).where(automation.c.name == PLANNER,
        run_table.c.orchestration == "objective", run_table.c.status == "active")).mappings()
    count = 0
    for raw in list(rules):
        rule = dict(raw)
        run = get(conn, "run", rule["run_id"])
        if run.get("pending_phase"):
            advance(scheduler, conn, run, now)
            continue
        observed = frontier(conn, run)
        if observed != rule["frontier_hash"] and rule.get("pause_reason") in (None, "idle"):
            rule = change(conn, "automation", rule["id"], frontier_hash=observed, no_progress_count=0, enabled=True, pause_reason=None, not_before=None)
            assignment = tables["assignment"]
            conn.execute(update(assignment).where(assignment.c.recurrence_key == f"planner:{run['id']}:next",
                assignment.c.status == "pending").values(pause_reason=None, not_before=None))
        if ensure_successor(scheduler, conn, run, rule):
            count += 1
    from ..review.demand import reconcile as reviews
    return {"planners": count, "reviews": reviews(scheduler, conn, actor, now)}


def accept_phase(scheduler, conn, actor, run, args):
    """Record a maintainer's semantic decision before changing phase inputs."""
    if run.get("orchestration") != "objective" or run["status"] != "active" or run.get("pending_phase"):
        raise DomainError("phase_not_active", "Accept one active objective phase at a time", 409)
    project_id = project_of(conn, "run", run["id"])
    if actor.kind == "agent":
        from ..auth import live_execution
        if live_execution(conn, actor)["run_id"] != run["id"]:
            raise DomainError("scope_mismatch", "Accept only the current objective", 403)
        current_id = live_execution(conn, actor)["assignment_id"]
    else:
        current_id = None
    for evidence in args["evidence"]:
        from ..persistence.records import same_project
        same_project(conn, "repository" if evidence["kind"] in ("file", "directory") else evidence["kind"],
            evidence.get("id", evidence.get("repository_id")), project_id)
    assignment = tables["assignment"]
    busy = select(assignment.c.id).where(assignment.c.run_id == run["id"], assignment.c.status.in_(LIVE),
        assignment.c.recurrence_key.is_distinct_from(f"planner:{run['id']}:next"))
    if current_id:
        busy = busy.where(assignment.c.id != current_id)
    if conn.execute(busy.limit(1)).first():
        raise DomainError("phase_work_active", "Account for other unfinished sessions before accepting this phase", 409)
    index = len(run["phase_history"])
    next_kind = run["requested_phases"][index + 1] if index + 1 < len(run["requested_phases"]) else None
    next_phase = args.get("next_phase")
    if next_phase and next_phase["kind"] != next_kind:
        raise DomainError("phase_order", "The next phase must match the requested objective sequence", 422)
    if next_kind == "preprocessing" and not next_phase:
        next_phase = {"kind": "preprocessing", "roadmap_document_id": str(run["objective_id"]), "orchestrated": False}
    if (next_kind == "formalization" and not next_phase
            and get(conn, "project", project_id)["workflow"] == "graph"):
        next_phase = {"kind": "formalization", "roadmap_snapshot_id": None, "orchestrated": False}
    if next_kind == "formalization" and not next_phase:
        baseline = tables["roadmap_snapshot"]
        doc = get(conn, "document", run["objective_id"])
        accepted = conn.execute(select(baseline).where(baseline.c.roadmap_document_id == doc["id"],
            baseline.c.status == "frozen", baseline.c.source_commit_oid == doc["source_commit_oid"])
            .order_by(baseline.c.created_at.desc()).limit(1)).mappings().first()
        if not accepted:
            raise DomainError("baseline_required", "Accept the verified current roadmap baseline before proof work", 409)
        from ..projects.milestones import require_baseline
        require_baseline(conn, accepted)
        next_phase = {"kind": "formalization", "roadmap_snapshot_id": str(accepted["id"]), "orchestrated": False}
    if next_kind == "postprocessing" and not next_phase:
        repo, workspace = tables["repository"], tables["workspace"]
        targets = list(conn.execute(select(repo.c.id).where(repo.c.project_id == project_id, repo.c.purpose == "library",
            repo.c.archived_at.is_(None))).scalars())
        sources = list(conn.execute(select(workspace).join(repo).where(workspace.c.project_id == project_id,
            workspace.c.status == "ready", repo.c.purpose == "workspace").order_by(workspace.c.updated_at.desc()).limit(1)).mappings())
        if len(targets) != 1 or not sources:
            raise DomainError("phase_inputs_required", "Select an exact source workspace/commit and destination library", 409)
        next_phase = {"kind": "postprocessing", "source_workspace_id": str(sources[0]["id"]),
            "source_commit_oid": sources[0]["head_commit_oid"] or sources[0]["base_commit_oid"],
            "target_repository_id": str(targets[0]), "orchestrated": False}
    if next_phase:
        # Validate explicit inputs with the same project/trust boundaries as a
        # launch; a phase transition is not a way to introduce a foreign repo.
        parsed = models.run_phase_adapter.validate_python(next_phase)
        if parsed.orchestrated:
            raise DomainError("retired_orchestrator", "Phase transitions use the same Work and Maintenance queues", 422)
        from ..persistence.records import same_project
        if parsed.kind == "preprocessing":
            document = same_project(conn, "document", parsed.roadmap_document_id, project_id)
            if document["kind"] != "roadmap" or document["id"] != run["objective_id"]:
                raise DomainError("objective_mismatch", "The next phase must retain the same roadmap objective", 422)
        elif parsed.kind == "formalization":
            if parsed.roadmap_snapshot_id:
                baseline = same_project(conn, "roadmap_snapshot", parsed.roadmap_snapshot_id, project_id)
                from ..projects.milestones import require_baseline
                require_baseline(conn, baseline)
                if baseline["roadmap_document_id"] != run["objective_id"]:
                    raise DomainError("objective_mismatch", "The next baseline belongs to another objective", 422)
            elif get(conn, "project", project_id)["workflow"] != "graph":
                raise DomainError("baseline_required", "This compatibility workflow requires a roadmap snapshot", 422)
        elif parsed.kind == "postprocessing":
            source = same_project(conn, "workspace", parsed.source_workspace_id, project_id)
            target = same_project(conn, "repository", parsed.target_repository_id, project_id)
            if target["purpose"] != "library" or source["status"] != "ready" or parsed.source_commit_oid not in (source["head_commit_oid"], source["base_commit_oid"]):
                raise DomainError("invalid_phase_input", "Postprocessing requires verified source and a library", 422)
        next_phase = parsed.model_dump(mode="json")
    history = run["phase_history"] + [{"phase": run["phase"], "objective_commit": get(conn, "document", run["objective_id"])["source_commit_oid"],
        "accepted_by": str(actor.id), "note": args["note"], "evidence": args["evidence"]}]
    row = change(conn, "run", run["id"], run["revision"], phase_history=history,
        pending_phase={**(next_phase or {"kind": "complete"}), **({"_owner_session_id": str(current_id)} if current_id else {})}, status="active" if run["auto_advance"] else "paused",
        status_note="Phase accepted; waiting for current session and deliveries to settle")
    pending = list(conn.execute(select(assignment).where(assignment.c.recurrence_key == f"planner:{run['id']}:next",
        assignment.c.status == "pending")).mappings())
    for item in pending:
        change(conn, "assignment", item["id"], status="cancelled", finished_at=func.now(), status_note="Phase accepted; successor superseded")
        change(conn, "mission", item["mission_id"], status="cancelled", closed_at=func.now(), closure_note="Unstarted planning pass superseded by phase acceptance")
    emit(conn, actor.id, project_id, "run", row, ["phase_history", "pending_phase"], note=args["note"])
    return row


def advance(scheduler, conn, run, now):
    assignment, execution = tables["assignment"], tables["execution"]
    if conn.execute(select(assignment.c.id).where(assignment.c.run_id == run["id"],
            assignment.c.status.in_(LIVE)).limit(1)).first():
        return  # An interrupted accepting maintainer must settle its same session.
    if conn.execute(select(execution.c.id).join(assignment).where(assignment.c.run_id == run["id"],
            scheduler.execution_busy_condition()).limit(1)).first():
        return
    owners = list(conn.execute(select(assignment.c.id).where(assignment.c.run_id == run["id"])).scalars())
    if any(scheduler.service.pending_deliveries(conn, owner) for owner in owners):
        return
    next_phase = {key: value for key, value in run["pending_phase"].items() if not key.startswith("_")}
    if next_phase["kind"] == "complete":
        from ..missions.mission_tree import ensure_closure_allowed
        root = get(conn, "mission", run["mission_id"])
        try:
            ensure_closure_allowed(conn, root)
        except DomainError:
            return  # Exposed as pending acceptance; a maintainer must account for children.
        change(conn, "mission", root["id"], status="completed", closed_at=now, closure_note=run["phase_history"][-1]["note"])
        change(conn, "run", run["id"], pending_phase=None, status="draining", status_note="Requested phases accepted; settling objective")
        return
    baseline = UUID(next_phase["roadmap_snapshot_id"]) if next_phase.get("roadmap_snapshot_id") else None
    row = change(conn, "run", run["id"], phase=next_phase, adopted_roadmap_snapshot_id=baseline,
        pending_phase=None, status_note="Accepted next phase; planner owns the next bounded decision")
    automation = tables["automation"]
    rule = conn.execute(select(automation).where(automation.c.run_id == run["id"], automation.c.name == PLANNER)).mappings().one()
    rule = change(conn, "automation", rule["id"], enabled=True, pause_reason=None, no_progress_count=0, not_before=None, frontier_hash=frontier(conn, row))
    ensure_successor(scheduler, conn, row, rule)


def finish_values(scheduler, conn, item, run, status, failure, now, *, stopping, reason, journal_pending):
    """Settle an execution without replacing its durable session or retry ledger."""
    from .queue_policies import category, policy
    from ..missions.conditions import Truth
    settings = policy(run, category(item))
    retry = settings.retry_policy.model_dump() if settings else run["retry_policy"]
    values = {"status": "pending", "finished_at": None, "retry_at": None, "pause_reason": None}
    if stopping or status == "cancelled":
        return values | {"status": "cancelled", "finished_at": now}
    if status not in ("succeeded", "yielded"):
        attempts = item["recovery_attempts"] + 1
        code = (failure or {}).get("code", "execution_failed")
        from .scheduler import TRANSIENT_CODES
        values["recovery_attempts"] = attempts
        thread = tables["provider_thread"]
        context = conn.execute(select(thread.c.status).where(thread.c.assignment_id == item["id"],
            thread.c.kind == "primary").order_by(thread.c.number.desc()).limit(1)).scalar_one_or_none()
        if context == "unavailable":
            return values | {"pause_reason": "Native context is unavailable; recover_context requires a diagnosis and confirmed physical stop", "status_note": "Session and retry history preserved"}
        if code in TRANSIENT_CODES and attempts <= retry["max_recovery_attempts"]:
            delay = min(retry["max_delay_seconds"], retry["initial_delay_seconds"] * 2 ** min(attempts - 1, 20))
            return values | {"retry_at": now + timedelta(seconds=random.uniform(delay / 2, delay)),
                "status_note": f"Resume this session after {code}; recovery attempt {attempts}"}
        return values | {"pause_reason": (failure or {}).get("message", "Recovery limit reached; diagnose before resuming"),
                         "status_note": "Session preserved; automatic recovery paused"}
    if journal_pending:
        return values | {"not_before": now + timedelta(seconds=60), "status_note": "Waiting to reconcile original API intents"}
    if scheduler.service.pending_deliveries(conn, item["id"]):
        return values | {"checkpoint_requested_at": now, "status_note": "Waiting for durable delivery; retained session will resume"}
    findings = scheduler.service.completion_findings(conn, item["id"])
    if not findings:
        return values | {"status": "completed", "finished_at": now, "checkpoint_requested_at": None,
                         "start_condition": None, "not_before": None, "status_note": "Bounded session accounted for; mission acceptance is separate"}
    if reason in ("execution_budget_reached", "request_budget_reached"):
        return values | {"pause_reason": "Execution budget reached; preserved work requires a recorded recovery decision"}
    if item["checkpoint_requested_at"] and scheduler.service.condition_readiness(conn, item, now, events_only=True).truth is Truth.FALSE:
        return values | {"status_note": "Waiting for the recorded event, including failed/cancelled outcomes"}
    if scheduler.service.progress_stalled(conn, item["id"], retry["max_no_progress_requests"]):
        return values | {"pause_reason": "Repeated continuations have not accounted for outstanding work", "status_note": "; ".join(findings)}
    return values | {"not_before": now + timedelta(seconds=retry["initial_delay_seconds"]), "status_note": "; ".join(findings)}


def request_maintenance(scheduler, conn, actor, run, args):
    """Ask for a bounded privileged decision without granting the caller authority."""
    if run.get("orchestration") != "objective" or run["status"] != "active" or run.get("pending_phase"):
        raise DomainError("objective_not_active", "Maintenance requests need an active objective", 409)
    if actor.kind == "agent":
        from ..auth import live_execution
        if live_execution(conn, actor)["run_id"] != run["id"]:
            raise DomainError("scope_mismatch", "Request maintenance only in your objective", 403)
    from ..persistence.records import same_project
    project_id = project_of(conn, "run", run["id"])
    for evidence in args["evidence"]:
        same_project(conn, "repository" if evidence["kind"] in ("file", "directory") else evidence["kind"],
                     evidence.get("id", evidence.get("repository_id")), project_id)
    key = "maintenance:" + str(run["id"]) + ":" + hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()[:32]
    table = tables["assignment"]
    existing = conn.execute(select(table).where(table.c.recurrence_key == key).order_by(table.c.created_at.desc()).limit(1)).mappings().first()
    if existing:
        return dict(existing)  # Repeated requests do not buy a fresh failure budget.
    from .queue_policies import validate_enqueue
    data = models.AssignmentCreate(run_id=run["id"], mission_id=run["mission_id"], role="maintainer", category="maintenance")
    validate_enqueue(conn, run, data)
    mission = child_mission(scheduler, conn, run, "Decide a maintenance request", args["note"],
        [template("objective/maintenance-acceptance")])
    row = scheduler.service.assignment(conn, scheduler.system_actor(conn), data.model_copy(update={
        "mission_id": mission["id"], "instructions": template("objective/maintenance-request", note=args["note"], evidence=json.dumps(args["evidence"]))}), internal=True)
    return change(conn, "assignment", row["id"], recurrence_key=key)
