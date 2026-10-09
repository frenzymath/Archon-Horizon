"""Versioned instructions and review descriptors; phase behavior is configuration."""

from __future__ import annotations

import json

from sqlalchemy import func, select

from ..persistence.records import json_value
from ..review.guidance import descriptors
from ..persistence.schema import tables
from .templates import template

# Keep these exports for older callers and stored catalogs. Their editable
# instruction bodies live in Markdown, alongside the rest of the prompt catalog.
SESSION_HANDOFF = template("session-handoff")
CORE = template("core")
PLANNER_RECOVERY = template("planner-recovery")
PLANNER = template("planner")
MAINTAINER = template("maintainer")

REVIEWERS = descriptors()

ORCHESTRATOR = template("legacy/orchestrator")

MISSION_PROMPT_CHARS = 6000
INSTRUCTIONS_PROMPT_CHARS = 4000
LEDGER_PROMPT_ROWS = 24
LEDGER_PROMPT_CHARS = 6000
# Character budgets bound inline context, not the persisted mission or ledger.
# Keep a small number of current items readable; linked records preserve every
# omitted requirement. LEDGER_PROMPT_CHARS includes row identifiers and markup.
OBJECTIVE_CORE = template("objective-core")
OBJECTIVE_PLANNER = template("objective-planner")
OBJECTIVE_MAINTAINER = template("objective-maintainer")


def catalog(skill_source_root=None) -> dict:
    """Snapshot the installed prompts and resources for a new provider context."""
    from .bundles import skill_files
    return {"schema_version": 1,
            "objective": {"core": OBJECTIVE_CORE, "planner": OBJECTIVE_PLANNER,
                          "maintainer": OBJECTIVE_MAINTAINER},
            "phase_contracts": dict(PHASE_CONTRACTS), "core": CORE,
            "functions": {"planner": PLANNER,
            "reviewer": template("reviewer/function"),
            "debugger": template("debugger"),
            "orchestrator": ORCHESTRATOR},
            "maintainer": MAINTAINER, "reviewers": REVIEWERS, **skill_files(skill_source_root)}


OPERATIONS_CONTEXT_BYTES = 16384
ORCHESTRATOR_CONTEXT_BYTES = OPERATIONS_CONTEXT_BYTES


def _phase_repositories(conn, run, project_id):
    from ..persistence.records import get

    repository = tables["repository"]
    query = select(repository.c.id).where(repository.c.project_id == project_id,
        repository.c.archived_at.is_(None), repository.c.purpose != "reference")
    phase = run["phase"]
    if phase.get("target_repository_id"):
        return query.where(repository.c.id == phase["target_repository_id"]), "phase target repository"
    document_id = phase.get("roadmap_document_id")
    if phase["kind"] == "formalization":
        baseline_id = run.get("adopted_roadmap_snapshot_id") or phase.get("roadmap_snapshot_id")
        if baseline_id:
            document_id = get(conn, "roadmap_snapshot", baseline_id)["roadmap_document_id"]
        elif run.get("orchestration") == "objective":
            document_id = run["objective_id"]
    if document_id:
        repository_id = get(conn, "document", document_id)["source_repository_id"]
        return query.where(repository.c.id == repository_id), (
            "adopted roadmap repository" if phase["kind"] == "formalization" else "phase roadmap repository")
    return query, "project repositories"


def _review_readiness_batch(conn, run, project_id, *, limit=4):
    from ..review.decisions import review_readiness

    repositories, _ = _phase_repositories(conn, run, project_id)
    item = tables["forge_item"]
    result = []
    for row in conn.execute(select(item).where(item.c.repository_id.in_(repositories),
            item.c.kind == "pull_request", item.c.status == "open")
            .order_by(item.c.created_at, item.c.remote_number).limit(limit)).mappings():
        readiness = review_readiness(conn, dict(row))
        result.append({"id": row["id"], "number": row["remote_number"], "head": row["head_commit_oid"],
            "target_branch": row["target_branch"],
            "readiness": {key: readiness[key] for key in
                ("ready", "required_dimensions", "missing_dimensions", "blockers", "next_actions")},
            "detail_url": f"/api/v3/forge-items/{row['id']}/inspect?view=reviews&expected_head_oid={row['head_commit_oid']}"})
    return result


def operations_context(conn, service, assignment, *, mission=None, run=None, view="operations"):
    """Expose operational state and bounded productive-owner scope."""
    from ..execution.context_briefing import columns, record
    from ..execution.coordination import _profile, summary
    from ..execution.notifications import control_summary
    from ..persistence.records import get
    from ..review.backlog import current_objection
    from ..execution.scheduler import Scheduler
    from ..operations.health_control import supervision_activity
    from ..execution.coordination_memory import state as recovery_state, follow_up_status
    from ..dashboard.activity_display import safe_text

    def size(value):
        # Bound the agent CLI's indented ASCII JSON, which is larger than the
        # compact UTF-8 representation used by the API and initial prompt.
        return len(json.dumps(json_value(value), ensure_ascii=True, indent=2).encode("utf-8"))

    def sanitized(value, limit=320):
        return safe_text(value, 65536).encode("utf-8")[:limit].decode(errors="ignore")

    mission = mission or get(conn, "mission", assignment["mission_id"])
    run = run or get(conn, "run", assignment["run_id"])
    project_id = mission["project_id"]
    state = summary(conn, service, run)
    global_state = state["global"]
    result = {
        "view": view, "schema_version": 1, "byte_budget": OPERATIONS_CONTEXT_BYTES,
        "assignment": record(assignment, "assignment", (
            "id", "number", "revision", "run_id", "mission_id", "role", "functions", "status")),
        "mission": record(mission, "mission", ("id", "revision", "project_id", "title", "status"), text_limit=200),
        "run": record(run, "run", ("id", "number", "revision", "status")),
        "phase": run["phase"]["kind"],
        "coordination": {"admission": state["admission"], "assignments": state["assignments"],
            "global": {key: global_state[key] for key in (
                "status", "reasons", "capacity", "maintainer_admission", "duplicate_automations")},
            "occupancy": global_state["active"]["live_by_role"],
            "pending": {key: global_state["pending"][key] for key in ("total", "oldest_age_seconds")}},
        "control_notices": control_summary(conn, assignment["id"]),
        "automations": [], "assignments": [], "workspaces": [], "milestone_jobs": [], "recent_failures": [],
        "hosts": [], "executions": [], "review_readiness": [], "collections": {},
    }
    result["assignment"]["profile"] = _profile(assignment)
    automation = tables["automation"]
    fields = ("id", "revision", "name", "role", "functions", "enabled", "start_condition",
              "not_before", "cooldown_seconds")
    query = select(*columns(automation, fields)).where(automation.c.run_id == run["id"])
    result["collections"]["automations"] = {
        "total": conn.execute(select(func.count()).select_from(query.subquery())).scalar_one(),
        "list_url": f"/api/v3/records/automation?run_id={run['id']}"}
    for row in conn.execute(query.order_by(automation.c.name).limit(8)).mappings():
        result["automations"].append({**record(row, "automation", fields), "profile": _profile(row)})

    owner = tables["assignment"]
    query = select(owner).where(owner.c.run_id == run["id"], owner.c.status.in_(("pending", "running", "stopping")))
    total = conn.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    rows = list(conn.execute(query.order_by((owner.c.status == "running").desc(), owner.c.number).limit(12)).mappings())
    mission_table = tables["mission"]
    mission_fields = ("id", "number", "revision", "title", "objective")
    productive_missions = {row["mission_id"] for row in rows if _profile(row) != "orchestrator"}
    owner_missions = {row["id"]: record(row, "mission", mission_fields, text_limit=384)
        for row in conn.execute(select(*columns(mission_table, mission_fields, text_limit=384)).where(
            mission_table.c.id.in_(productive_missions))).mappings()}
    scheduler = Scheduler(service)
    observations = scheduler.admission_observations(conn, rows)
    now = conn.execute(select(func.now())).scalar_one()
    activity = supervision_activity(conn, run["id"], [row["id"] for row in rows])
    event = tables["activity"]
    latest = {row["assignment_id"]: row for row in conn.execute(select(
        event.c.id, event.c.assignment_id, event.c.kind, event.c.occurred_at,
        func.left(event.c.summary, 65536).label("summary")).where(
            event.c.assignment_id.in_([row["id"] for row in rows]), event.c.summary.is_not(None),
            event.c.kind.in_(("checkpoint", "progress", "tool_use", "completion", "failure")))
        .distinct(event.c.assignment_id)
        .order_by(event.c.assignment_id, event.c.created_at.desc(), event.c.id.desc())).mappings()}
    watch = recovery_state(conn, run["id"])
    result["progress"] = {"last_evidence_change_at": watch["last_progress_at"] if watch else None,
        "unchanged_seconds": max(0, int((now - watch["last_progress_at"]).total_seconds())) if watch else None,
        "audit_obligation_id": watch["audit_obligation_id"] if watch else None,
        "note": "Work-accounted timestamps are owner claims; artifact/review evidence and recovery outcomes must confirm progress."}
    if watch and watch["audit_obligation_id"] and watch["audit_frontier_hash"] == watch["frontier_hash"]:
        recovery = follow_up_status(conn, run["id"], get(conn, "obligation", watch["audit_obligation_id"]), now,
                                    timeout_seconds=service.config.coordination_stall_seconds)
        result["progress"]["recovery"] = {**recovery, "owners": recovery["owners"][:8],
            "issues": [issue[:240] for issue in recovery["issues"][:8]]}
    for row in rows:
        profile = _profile(row)
        fields = ("id", "number", "revision", "role", "functions", "status",
            "automation_id", "not_before", "retry_at", "start_condition")
        if profile != "orchestrator":
            fields += ("instructions",)
        item = record(row, "assignment", fields, text_limit=512)
        item["profile"] = profile
        if item["profile"] != "orchestrator":
            item["mission"] = owner_missions[row["mission_id"]]
        item["activity"] = activity[row["id"]]
        item["activity_url"] = f"/api/v3/dashboard/activity/sessions/{row['id']}/events?compact=true&limit=10"
        recent = latest.get(row["id"])
        item["last_activity"] = ({
            "id": recent["id"], "kind": recent["kind"], "occurred_at": recent["occurred_at"],
            "summary": sanitized(recent["summary"]),
            "detail_url": f"/api/v3/dashboard/activity/sessions/{row['id']}/events/{recent['id']}",
        } if recent else None)
        if row["status"] == "pending":
            ready = service.readiness(conn, row, now, observations=observations)
            item["waiting_reason"] = (scheduler.admission_blocker(conn, row, observations=observations)
                                      if ready.ready else ready.reason)
        result["assignments"].append(item)
    result["collections"]["assignments"] = {"total": total,
        "list_url": f"/api/v3/assignments?run_id={run['id']}&limit=20",
        "running_url": f"/api/v3/assignments?run_id={run['id']}&status=running&limit=20",
        "pending_url": f"/api/v3/assignments?run_id={run['id']}&status=pending&limit=20"}

    workspace = tables["workspace"]
    fields = ("id", "revision", "repository_id", "host_id", "status", "updated_at")
    query = select(*columns(workspace, fields)).where(
        workspace.c.project_id == project_id, workspace.c.status != "retired")
    result["collections"]["workspaces"] = {
        "total": conn.execute(select(func.count()).select_from(query.subquery())).scalar_one(),
        "list_url": f"/api/v3/records/workspace?project_id={project_id}"}
    for row in conn.execute(query.order_by((workspace.c.status == "preparing").desc(), workspace.c.updated_at).limit(8)).mappings():
        result["workspaces"].append(record(row, "workspace", fields))
    job = tables["milestone_job"]
    fields = ("id", "revision", "host_id", "workspace_id", "status", "attempts", "lease_until", "updated_at", "error")
    counts = dict(conn.execute(select(job.c.status, func.count()).where(
        job.c.project_id == project_id).group_by(job.c.status)).all())
    result["collections"]["milestone_jobs"] = {"scope": "project", "project_id": project_id,
        "total": sum(counts.values()), "by_status": counts,
        "detail_url_template": "/api/v3/milestones/verifications/{id}",
        "selection_note": "At most 8 jobs, active first then recently updated. No paginated job list is available; "
                          "use a known job ID for its full record."}
    query = select(*columns(job, fields, text_limit=320)).where(job.c.project_id == project_id)
    for row in conn.execute(query.order_by(job.c.status.in_(("queued", "running")).desc(),
            job.c.updated_at.desc(), job.c.id).limit(8)).mappings():
        item = record(row, "milestone_job", fields, text_limit=320)
        item["error"] = sanitized(row["error"]) or None
        item["detail_url"] = f"/api/v3/milestones/verifications/{row['id']}"
        result["milestone_jobs"].append(item)
    execution = tables["execution"]
    fields = ("id", "assignment_id", "status", "heartbeat_at", "lease_expires_at")
    for row in conn.execute(select(*columns(execution, fields)).join(owner).where(
            owner.c.run_id == run["id"], execution.c.status.in_(("starting", "running", "stopping")))
            .order_by(execution.c.started_at.desc()).limit(8)).mappings():
        result["executions"].append(record(row, "execution", fields))
    fields = ("id", "assignment_id", "status", "finished_at", "failure")
    for row in conn.execute(select(*columns(execution, fields)).join(owner).where(
            owner.c.run_id == run["id"], execution.c.failure.is_not(None))
            .order_by(execution.c.created_at.desc()).limit(3)).mappings():
        failure = record(row, "execution", fields)
        failure["failure"] = {key: sanitized((row["failure"] or {}).get(key))
                              for key in ("code", "message") if (row["failure"] or {}).get(key)}
        result["recent_failures"].append(failure)
    host, run_host = tables["host"], tables["run_host"]
    for row in conn.execute(select(host.c.id, host.c.mode, host.c.heartbeat_at, host.c.health)
            .join(run_host).where(run_host.c.run_id == run["id"], run_host.c.enabled.is_(True))
            .order_by(host.c.id).limit(8)).mappings():
        health = row["health"] or {}
        result["hosts"].append({"id": row["id"], "mode": row["mode"], "heartbeat_at": row["heartbeat_at"],
            "health": {key: health.get(key) for key in ("status", "free_bytes", "required_free_bytes")}})

    forge = tables["forge_item"]
    repositories, scope = _phase_repositories(conn, run, project_id)
    backlog = select(forge.c.id).where(forge.c.repository_id.in_(repositories),
        forge.c.kind == "pull_request", forge.c.status == "open")
    result["forge"] = {
        "open_pull_requests": conn.execute(select(func.count()).select_from(backlog.subquery())).scalar_one(),
        "actionable_pull_requests": conn.execute(select(func.count()).select_from(
            backlog.where(~current_objection(forge)).subquery())).scalar_one(),
        "scope": scope,
    }
    result["review_readiness"] = _review_readiness_batch(conn, run, project_id)
    for item in result["review_readiness"]:
        actions = item["readiness"].pop("next_actions")
        owners = [action for action in actions if action.get("owner_id")]
        item["reviewer_owners"] = [{
            **{key: action[key] for key in ("reviewer_descriptor_id", "dimensions", "owner_id", "owner_kind", "owner_status")},
            "detail_url": f"/api/v3/records/{action['owner_kind']}/{action['owner_id']}",
        } for action in owners[:8]]
        item["reviewer_owner_count"] = len(owners)
        item["reviewer_owners_truncated"] = len(owners) > 8
    result["collections"]["review_readiness"] = {"total": result["forge"]["open_pull_requests"],
        "list_url": f"/api/v3/forge-items?project_id={project_id}&q=open&limit=20"}

    # A registered exact topic is the explicit reporting convention. Never infer
    # a destination from arbitrary topics, subscriptions, or another project.
    discussion, integration = tables["discussion"], tables["integration"]
    bindings = list(conn.execute(select(discussion.c.id, discussion.c.sync_status).join(integration).where(
        discussion.c.project_id == project_id, discussion.c.topic == "Horizon operations",
        integration.c.kind == "zulip", integration.c.enabled.is_(True)).limit(2)).mappings())
    result["operations_reporting"] = ({"status": "configured", "discussion_id": bindings[0]["id"],
        "sync_status": bindings[0]["sync_status"]} if len(bindings) == 1 else {
        "status": "unconfigured" if not bindings else "ambiguous", "discussion_id": None,
        "reason": "An operator must register exactly one 'Horizon operations' topic in this project's bound Zulip channel. "
                  "Do not discover another destination or post to mathematical topics."})
    result["context_note"] = ("Read-only operational snapshot. Profiles override stored roles. Activity timestamps and "
        "owner claims are not proof of useful progress; check linked results. Other projects contribute only aggregate "
        "shared capacity counts. Record access and mutation authority are unchanged.")
    for key in ("recent_failures", "executions", "hosts"):
        result["collections"][key] = {"selected": len(result[key]),
            "detail_url": f"/api/v3/assignments/{assignment['id']}/context"}
    # Preserve typed conditions whole and retain collection locators on overflow.
    for key in ("workspaces", "executions", "hosts", "milestone_jobs", "recent_failures", "automations", "review_readiness", "assignments"):
        while result[key] and size(result) > OPERATIONS_CONTEXT_BYTES - 256:
            result[key].pop()
    if size(result) > OPERATIONS_CONTEXT_BYTES - 256:
        notices = result["control_notices"]
        notices["omitted"], notices["notices"] = notices["pending"], []
    for key, collection in result["collections"].items():
        collection["included"] = len(result[key])
        collection["truncated"] = collection.get("total", collection.get("selected", 0)) > len(result[key])
    if size(result) > OPERATIONS_CONTEXT_BYTES:
        result["coordination"] = {"truncated": True, "detail_url": f"/api/v3/assignments/{assignment['id']}/context",
            "admission": {key: state["admission"].get(key) for key in ("limit", "remaining_new_assignments")},
            "global": {"status": global_state["status"], "capacity": global_state["capacity"]}}
    return result


def orchestrator_context(conn, service, assignment, *, mission=None, run=None):
    return operations_context(conn, service, assignment, mission=mission, run=run, view="orchestrator")


def _orchestrator_goal(conn, service, assignment, run, mission, findings):
    context = orchestrator_context(conn, service, assignment, mission=mission, run=run)
    pinned = _pinned_bundle(conn, service, assignment)
    parts = [pinned.get("functions", {}).get("orchestrator", template("legacy/orchestrator", pinned)), f"Assignment R{run['number']}/A{assignment['number']}",
             "Current control snapshot:\n" + json.dumps(json_value(context), ensure_ascii=False, separators=(",", ":"))]
    if findings:
        parts.append("Control continuation findings:\n" + "\n".join(item[:400] for item in findings[:3]))
    return "\n\n".join(parts)


PHASE_CONTRACTS = {phase: template("legacy/phase-" + phase) for phase in ("preprocessing", "formalization", "postprocessing")}


def _pinned_bundle(conn, service, assignment):
    """Read retained instructions; the installed catalog is only for first entry."""
    from ..persistence.records import get
    thread = tables["provider_thread"]
    current = conn.execute(select(thread).where(thread.c.assignment_id == assignment["id"],
        thread.c.kind == "primary", thread.c.status.in_(("creating", "available")))).mappings().first()
    if current:
        artifact = get(conn, "artifact", current["skill_bundle_artifact_id"])
        return json.loads(service.store.read(artifact["content"]["sha256"]))
    return catalog(service.config.skill_source_root)


def objective_goal(conn, service, assignment, run, mission, *, findings, initial):
    """Keep the native goal useful to a reader, with bounded Markdown commitments."""
    from ..persistence.records import get
    from .bundles import skill_path
    from ..execution.notifications import prompt_summary
    pinned = _pinned_bundle(conn, service, assignment)
    profile = pinned.get("objective") or {key: template("objective-" + key, pinned)
                                          for key in ("core", "planner", "maintainer")}
    document = get(conn, "document", run["objective_id"])
    parts = [profile["core"] if initial else template("objective-continuation", pinned),
        f"Objective {document['id']} (revision {document['revision']}, source {document['source_commit_oid']}). Read its versioned roadmap source.",
        f"Mission {mission['id']} revision {mission['revision']}: {mission['title']}", mission["objective"][:MISSION_PROMPT_CHARS],
        "Acceptance criteria:\n" + "\n".join("- " + value for value in mission["acceptance_criteria"])[:MISSION_PROMPT_CHARS],
        f"Full mission and criteria: GET /api/v3/records/mission/{mission['id']}; read omitted scope before claiming completion.",
        f"Phase: {run['phase']['kind']}; category: {assignment['category']}; role: {assignment['role']}"]
    if initial:
        parts += [f"Entrypoint: $HORIZON_SKILLS_DIR/{skill_path(pinned, 'horizon-pipeline')}",
                  f"Phase guidance: $HORIZON_SKILLS_DIR/{skill_path(pinned, 'horizon-main-work' if run['phase']['kind'] == 'formalization' else 'horizon-' + run['phase']['kind'])}"]
    parts.append("Pinned phase inputs: " + json.dumps(json_value(run["phase"])))
    if assignment["instructions"] and assignment["instructions"] != mission["objective"]:
        parts.append(assignment["instructions"][:INSTRUCTIONS_PROMPT_CHARS])
    if initial and "planner" in assignment["functions"]:
        parts.append(profile["planner"])
    if initial and assignment["role"] == "maintainer":
        parts.append(profile["maintainer"])
    obligation = tables["obligation"]
    rows = list(conn.execute(select(obligation.c.id, obligation.c.description).where(
        obligation.c.assignment_id == assignment["id"], obligation.c.status == "open")
        .order_by(obligation.c.number).limit(LEDGER_PROMPT_ROWS + 1)).mappings())
    ledger, used = [], 0
    for row in rows[:LEDGER_PROMPT_ROWS]:
        description = row["description"]
        if description == mission["objective"]:
            description = "The mission above; this obligation remains open."
        suffix = f" (item {row['id']})"
        room = min(1000, LEDGER_PROMPT_CHARS - used - len(suffix) - len("- [ ] \n"))
        if room < 32:
            break
        if len(description) > room:
            description = description[:room - len(" [excerpt]")] + " [excerpt]"
        line = "- [ ] " + description + suffix
        ledger.append(line)
        used += len(line) + 1
    parts.append("Open goal ledger:\n" + ("\n".join(ledger) or "No open ledger items; mission acceptance remains separate."))
    if len(rows) > len(ledger) or any(" [excerpt]" in line for line in ledger):
        parts.append(f"Ledger excerpt: page /api/v3/records/obligation?assignment_id={assignment['id']}&limit=20 before claiming completion.")
    if findings:
        parts.append("Outstanding completion findings:\n" + "\n".join(findings))
    notices = prompt_summary(conn, assignment["id"])
    if notices:
        parts.append(notices)
    return "\n\n".join(parts)


def goal(conn, service, assignment, run, mission, *, findings=None, initial=True) -> str:
    from ..execution.notifications import prompt_summary

    if "orchestrator" in (assignment.get("functions") or []):
        return _orchestrator_goal(conn, service, assignment, run, mission, findings)
    if assignment["reviewer_descriptor_id"]:
        # Prepared reviews already carry the exact scope, rubric and lifecycle.
        from ..review.invocations import assignment_manifest
        manifest = assignment_manifest(conn, service, assignment)[1]
        from ..persistence.records import get
        bundle = get(conn, "artifact", manifest["skill_bundle_artifact_id"])
        pinned = json.loads(service.store.read(bundle["content"]["sha256"]))
        parts = [manifest["prompt"] if initial else template("reviewer/continuation", pinned)]
        if findings:
            parts.append("Completion findings:\n" + "\n".join(findings))
        notices = prompt_summary(conn, assignment["id"])
        if notices:
            parts.append(notices)
        return "\n\n".join(parts)

    if run.get("orchestration") == "objective":
        return objective_goal(conn, service, assignment, run, mission, findings=findings, initial=initial)

    from ..persistence.records import get
    pinned = _pinned_bundle(conn, service, assignment)
    obligation = tables["obligation"]
    rows = [dict(row) for row in conn.execute(select(obligation).where(
        obligation.c.assignment_id == assignment["id"]).order_by((obligation.c.status == "open").desc(),
            obligation.c.number).limit(LEDGER_PROMPT_ROWS + 1)).mappings()]
    complete_context = {"kind": "assignment_prompt_context", "version": 1,
        "mission": {key: mission[key] for key in ("id", "revision", "title", "objective")},
        "assignment": {key: assignment[key] for key in ("id", "revision", "instructions")},
        "obligations": [{key: row[key] for key in ("id", "number", "revision", "description", "status")}
                        for row in rows[:LEDGER_PROMPT_ROWS] if initial or row["status"] == "open"]}
    excerpted = False

    def excerpt(value, limit):
        nonlocal excerpted
        if len(value) <= limit:
            return value
        excerpted = True
        return value[:limit] + "\n[Excerpt; full text is preserved in the linked prompt-context snapshot below.]"

    parts = [pinned["core"] if initial else template("continuation", pinned),
             f"Assignment R{run['number']}/A{assignment['number']} ({assignment['id']})",
             f"Phase: {run['phase']['kind']}; role: {assignment['role']}",
             f"Mission revision {mission['revision']}: {mission['title']}",
             excerpt(mission["objective"], MISSION_PROMPT_CHARS),
             "Run inputs (authoritative repository and source scope):\n" + json.dumps(json_value(run["phase"]), indent=2),
             "Phase outcome: " + pinned.get("phase_contracts", PHASE_CONTRACTS)[run["phase"]["kind"]]]
    if assignment["instructions"] and assignment["instructions"] != mission["objective"]:
        parts.append("Your task:\n" + excerpt(assignment["instructions"], INSTRUCTIONS_PROMPT_CHARS))
    if initial:
        from .bundles import skill_path
        parts.append(f"Entrypoint: $HORIZON_SKILLS_DIR/{skill_path(pinned, 'horizon-pipeline')}" +
                     template("supporting-index", pinned))
        if "planner" in assignment["functions"]:
            parts.append(pinned["functions"]["planner"])
        if assignment["role"] == "maintainer":
            parts.append(pinned["maintainer"])
    recovery = template("planner-recovery", pinned)
    if "planner" in assignment["functions"] and (not initial or recovery not in pinned["functions"]["planner"]):
        parts.append(recovery)
    if assignment["role"] == "maintainer":
        readiness = _review_readiness_batch(conn, run, mission["project_id"])
        if readiness:
            parts.append("Current exact-head review readiness:\n" +
                json.dumps(json_value(readiness), ensure_ascii=False) +
                "\n" + template("review-readiness-followup", pinned))
    parts.append(template("session-handoff", pinned))
    ledger = []
    for source in complete_context["obligations"]:
        row = dict(source)
        if row["description"] == mission["objective"]:
            row["description"] = f"The mission objective above (revision {mission['revision']}); this obligation remains {row['status']}."
        else:
            row["description"] = excerpt(row["description"], 1000)
        # Charge escaped text and row metadata too. A Unicode-heavy ledger or
        # many small rows must not evade the aggregate inline-context budget.
        candidate = ledger + [row]
        while len(json.dumps(json_value(candidate), ensure_ascii=True)) > LEDGER_PROMPT_CHARS and row["description"]:
            excerpted = True
            row["description"] = row["description"][:max(0, len(row["description"]) - 64)]
        if len(json.dumps(json_value(candidate), ensure_ascii=True)) > LEDGER_PROMPT_CHARS:
            excerpted = True
            break
        ledger.append(row)
    parts.append("Current obligations:\n" + json.dumps(json_value(ledger), ensure_ascii=True))
    if len(rows) > LEDGER_PROMPT_ROWS or len(ledger) < len(complete_context["obligations"]):
        parts.append(f"Ledger excerpt capped at {LEDGER_PROMPT_ROWS}; page /api/v3/records/obligation?assignment_id="
                     + str(assignment["id"]) + " to inspect the complete ledger before accounting for completion.")
    if excerpted:
        from ..persistence.records import save_blob
        artifact = save_blob(conn, service.store, mission["project_id"], complete_context)
        parts.append(template("excerpted-context", pinned, artifact_id=artifact["id"]))
    if findings:
        parts.append("Continue this context. The completion check found:\n" + "\n".join(findings))
    notices = prompt_summary(conn, assignment["id"])
    if notices:
        parts.append(notices)
    return "\n\n".join(parts)
