"""Original desktop activity presentation, projected from current pipeline records."""

from __future__ import annotations

from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import JSONPATH

from . import readmodels
from .activity_display import safe_text
from .activity_readmodel import enrich, timeline
from .dashboard_conditions import admissions
from ..auth import require_project
from ..errors import DomainError
from ..persistence.records import get, project_of
from ..persistence.schema import tables
from ..providers.usage_accounting import trusted_amount, uncertain_usage

SESSION_PAGE_LIMIT = 100


def _profile(functions, role):
    """Expose the operational profile even when legacy role storage is used."""
    return "orchestrator" if "orchestrator" in (functions or []) else role


def _status(status):
    return {"active": "running", "pending": "queued", "completed": "succeeded", "stopping": "cancelling",
            "paused": "waiting", "draining": "waiting", "starting": "running", "submitted": "running",
            "uncertain": "waiting", "lost": "interrupted"}.get(status, status)


def _seconds(start, end, now):
    return max(0, ((end or now) - start).total_seconds()) if start else 0


def _usage(row=None):
    row = row or {}
    return {"tokens_in": row.get("input_tokens"), "tokens_out": row.get("output_tokens"),
            "cost_usd": row.get("cost_usd"), "incomplete": bool(row.get("usage_uncertain"))}


def _run_views(conn, rows, *, compact=False):
    if not rows:
        return []
    ids = [row["id"] for row in rows]
    assignment, execution, usage, thread, run_host, hh, harness = (tables[name] for name in (
        "assignment", "execution", "usage_record", "provider_thread", "run_host", "host_harness", "harness"))
    counts = {row["run_id"]: row for row in conn.execute(select(assignment.c.run_id,
        func.count().label("sessions"), func.count().filter(assignment.c.parent_id.is_(None)).label("main"),
        func.count().filter(assignment.c.status.in_(("running", "stopping"))).label("running"),
        func.count().filter(assignment.c.status == "pending").label("queued"))
        .where(assignment.c.run_id.in_(ids)).group_by(assignment.c.run_id)).mappings()}
    children = dict(conn.execute(select(assignment.c.run_id, func.count()).select_from(thread.join(assignment))
        .where(assignment.c.run_id.in_(ids), thread.c.kind == "child").group_by(assignment.c.run_id)).all()) if not compact else {}
    measured = {row["run_id"]: row for row in conn.execute(select(assignment.c.run_id,
        *[func.sum(trusted_amount(usage, key)).label(key) for key in ("input_tokens", "output_tokens", "cost_usd")],
        func.bool_or(uncertain_usage(usage)).label("usage_uncertain"))
        .select_from(usage.join(execution).join(assignment)).where(assignment.c.run_id.in_(ids))
        .group_by(assignment.c.run_id)).mappings()} if not compact else {}
    durations = dict(conn.execute(select(assignment.c.run_id, func.sum(func.extract("epoch",
        func.coalesce(execution.c.finished_at, func.now()) - execution.c.started_at)))
        .select_from(execution.join(assignment)).where(assignment.c.run_id.in_(ids), execution.c.started_at.is_not(None))
        .group_by(assignment.c.run_id)).all()) if not compact else {}
    capacities = dict(conn.execute(select(run_host.c.run_id, func.sum(hh.c.execution_slots))
        .select_from(run_host.join(hh, hh.c.host_id == run_host.c.host_id).join(harness))
        .where(run_host.c.run_id.in_(ids), run_host.c.enabled.is_(True), hh.c.enabled.is_(True), harness.c.enabled.is_(True))
        .group_by(run_host.c.run_id)).all())
    models = {row["run_id"]: row for row in conn.execute(select(assignment.c.run_id,
        func.array_agg(func.distinct(thread.c.applied_model_options["model"].astext)).label("models"),
        func.array_agg(func.distinct(thread.c.applied_model_options["reasoning_effort"].astext)).label("efforts"))
        .select_from(thread.join(assignment)).where(assignment.c.run_id.in_(ids)).group_by(assignment.c.run_id)).mappings()} if not compact else {}
    now = conn.execute(select(func.now())).scalar_one()
    result = []
    for row in rows:
        count = counts.get(row["id"], {})
        sessions, main = count.get("sessions", 0), count.get("main", 0)
        native = children.get(row["id"], 0)
        result.append({**row, "status": "paused" if row.get("pause_reason") else _status(row["status"]), "session_count": sessions,
            "main_session_count": main, "delegated_session_count": sessions - main,
            "running_session_count": count.get("running", 0), "queued_session_count": count.get("queued", 0),
            "native_subagent_count": native, "subagent_count": native,
            "total_session_count": sessions, "usage": _usage(measured.get(row["id"])),
            "elapsed_seconds": _seconds(row["started_at"] or row["created_at"], row["finished_at"], now),
            "agent_seconds": None if compact else float(durations.get(row["id"], 0) or 0),
            "metrics_pending": compact, "slot_capacity": capacities.get(row["id"], 0),
            "models": [model for model in models.get(row["id"], {}).get("models", []) if model],
            "efforts": [effort for effort in models.get(row["id"], {}).get("efforts", []) if effort],
            "stop_reason": row.get("status_note"), "context": {"mission_id": str(row["mission_id"]), "mission_title": row["title"]}})
    return result


def runs(conn, actor, *, project_id=None, status=None, before=None, limit=25, compact=False):
    run, mission, project = tables["run"], tables["mission"], tables["project"]
    visible = readmodels.visible_projects(conn, actor).with_only_columns(project.c.id)
    query = select(*_run_columns(run), mission.c.project_id, mission.c.title, project.c.title.label("project_title"))
    query = query.select_from(run.join(mission).join(project)).where(project.c.id.in_(visible))
    if project_id:
        require_project(conn, actor, project_id)
        query = query.where(project.c.id == project_id)
    if status:
        groups = {"running": ("active",), "succeeded": ("completed",), "cancelled": ("cancelled",),
                  "blocked": ("paused", "draining"), "queued": (), "failed": ()}
        if status not in groups:
            raise DomainError("invalid_status", "Unknown run status", 422)
        query = query.where(run.c.status.in_(groups[status]))
    page = readmodels.page(conn, query, run, before, limit, order="number", descending=True)
    return {"runs": _run_views(conn, page["items"], compact=compact), "next_before": page["next_cursor"]}


def _run_columns(run):
    return [run.c[key] for key in ("id", "number", "revision", "mission_id", "status", "phase",
        "created_at", "started_at", "finished_at", "status_note")]


def _pending_queue(run_id):
    """Rank the complete run queue before page or session filters are applied."""
    assignment = tables["assignment"]
    return select(assignment.c.id, func.row_number().over(
        order_by=(assignment.c.queue_rank, assignment.c.id)).label("_pending_position")).where(
        assignment.c.run_id == run_id, assignment.c.status == "pending").subquery()


def _session_summary_page(conn, run_id, *, before=None, limit=SESSION_PAGE_LIMIT):
    assignment, mission, thread = (tables[key] for key in ("assignment", "mission", "provider_thread"))
    retained = select(thread.c.id).where(thread.c.assignment_id == assignment.c.id, thread.c.kind == "primary").exists()
    pending = _pending_queue(run_id)
    query = select(*[assignment.c[key] for key in (
        "id", "revision", "parent_id", "number", "role", "functions", "status", "created_at", "started_at", "finished_at", "queue_rank", "pause_reason", "category")],
        func.left(mission.c.title, 512).label("title"), retained.label("retained"), pending.c._pending_position).join(mission).outerjoin(
        pending, pending.c.id == assignment.c.id).where(assignment.c.run_id == run_id)
    page = readmodels.page(conn, query, assignment, before, limit, order="number", descending=True)
    rows = page["items"]
    positions = {row["id"]: int(row["_pending_position"]) for row in rows if row["status"] == "pending"}
    sessions = [{"id": row["id"], "run_id": run_id, "revision": row["revision"], "parent_session_id": row["parent_id"],
        "label": str(row["number"]), "session_number": row["number"], "title": row["title"], "role": _profile(row["functions"], row["role"]),
        "functions": list(row["functions"] or []), "category": row["category"],
        "status": "paused" if row.get("pause_reason") else _status(row["status"]), "created_at": row["created_at"], "started_at": row["started_at"],
        "finished_at": row["finished_at"], "native": False, "queue_position": positions.get(row["id"]),
        "context": {"retained_continuation": row["status"] == "pending" and row["retained"]}}
        for row in rows]
    return sessions, page["next_cursor"]


def _session_summaries(conn, run_id):
    return _session_summary_page(conn, run_id)[0]


def _sessions_page(conn, run_id, assignment_id=None, *, service, before=None, limit=SESSION_PAGE_LIMIT):
    assignment, mission, execution, host, thread, request, usage, activity = (tables[name] for name in (
        "assignment", "mission", "execution", "host", "provider_thread", "provider_request", "usage_record", "activity"))
    pending = _pending_queue(run_id)
    query = select(assignment, mission.c.title, pending.c._pending_position).join(mission).outerjoin(
        pending, pending.c.id == assignment.c.id).where(assignment.c.run_id == run_id)
    if assignment_id:
        query = query.where(assignment.c.id == assignment_id)
    page = readmodels.page(conn, query, assignment, before, limit, order="number", descending=True)
    rows = page["items"]
    ids = [row["id"] for row in rows]
    attempts = {}
    for row in conn.execute(select(execution, host.c.display_name.label("host_name"))
        .join(host).where(execution.c.assignment_id.in_(ids)).order_by(execution.c.number)).mappings():
        attempts.setdefault(row["assignment_id"], []).append(dict(row))
    threads = [dict(row) for row in conn.execute(select(thread).where(thread.c.assignment_id.in_(ids))
                                               .order_by(thread.c.number)).mappings()]
    threads_by_assignment = {}
    for row in threads:
        threads_by_assignment.setdefault(row["assignment_id"], []).append(row)
    thread_ids = [row["id"] for row in threads]
    requests = [dict(row) for row in conn.execute(select(*[request.c[key] for key in (
        "id", "provider_thread_id", "number", "status", "reviewer_descriptor_revision_id", "started_at", "finished_at")])
        .where(request.c.provider_thread_id.in_(thread_ids))
                                                 .order_by(request.c.number)).mappings()]
    request_by_id = {row["id"]: row for row in requests}
    revision = tables["record_revision"]
    descriptor_ids = {row["reviewer_descriptor_revision_id"] for row in requests if row["reviewer_descriptor_revision_id"]}
    descriptors = {row["revision_id"]: row for row in conn.execute(select(revision.c.id.label("revision_id"),
        revision.c.content["id"].astext.label("id"), revision.c.content["revision"].as_integer().label("revision"),
        revision.c.content["slug"].astext.label("slug"),
        func.left(revision.c.content["instructions"].astext, 16000).label("instructions"))
        .where(revision.c.id.in_(descriptor_ids))).mappings()} if descriptor_ids else {}
    requests_by_thread = {}
    for row in requests:
        requests_by_thread.setdefault(row["provider_thread_id"], []).append(row)
    thread_by_id = {row["id"]: row for row in threads}
    usage_rows = {row["provider_thread_id"]: row for row in conn.execute(select(usage.c.provider_thread_id,
        *[func.sum(trusted_amount(usage, key)).label(key) for key in ("input_tokens", "output_tokens", "cost_usd")],
        func.bool_or(uncertain_usage(usage)).label("usage_uncertain"))
        .where(usage.c.provider_thread_id.in_(thread_ids)).group_by(usage.c.provider_thread_id)).mappings()}
    skills = {}
    for row in conn.execute(select(activity.c.assignment_id, func.unnest(activity.c.skills_used).label("skill"))
                            .where(activity.c.assignment_id.in_(ids))).mappings():
        skills.setdefault(row["assignment_id"], set()).add(row["skill"])
    now = conn.execute(select(func.now())).scalar_one()
    positions = {row["id"]: int(row["_pending_position"]) for row in rows if row["status"] == "pending"}
    admission = admissions(conn, rows, service, now)
    result = []
    for row in rows:
        own_threads = [item for item in threads_by_assignment.get(row["id"], []) if item["kind"] == "primary"]
        own_attempts = attempts.get(row["id"], [])
        options = [item["applied_model_options"] for item in own_threads]
        summed = {}
        for key in ("input_tokens", "output_tokens", "cost_usd"):
            known = [usage_rows[item["id"]][key] for item in own_threads if item["id"] in usage_rows and usage_rows[item["id"]][key] is not None]
            summed[key] = sum(known) if known else None
        summed["usage_uncertain"] = any(usage_rows.get(item["id"], {}).get("usage_uncertain") for item in own_threads)
        session = {"id": row["id"], "run_id": run_id, "revision": row["revision"], "parent_session_id": row["parent_id"],
            "label": str(row["number"]), "session_number": row["number"], "title": row["title"],
            "role": _profile(row["functions"], row["role"]), "functions": list(row["functions"] or []), "status": "paused" if row.get("pause_reason") else _status(row["status"]), "created_at": row["created_at"],
            "started_at": row["started_at"], "finished_at": row["finished_at"], "usage": _usage(summed),
            "models": sorted({item["model"] for item in options if item.get("model")}),
            "efforts": sorted({item["reasoning_effort"] for item in options if item.get("reasoning_effort")}),
            "skills": sorted(skills.get(row["id"], [])), "subagents": [], "host_id": own_attempts[-1]["host_name"] if own_attempts else None,
            "profile_id": _profile(row["functions"], row["role"]), "elapsed_seconds": _seconds(row["started_at"], row["finished_at"], now),
            "agent_seconds": sum(_seconds(item["started_at"], item["finished_at"], now) for item in own_attempts),
            "attempt_count": len(own_attempts), "native": False, "queue_position": positions.get(row["id"]),
            "queue_priority_name": "current", "can_resume": row["status"] == "failed" or bool(row.get("pause_reason")),
            "context": {"mission_id": str(row["mission_id"]), "mission_title": row["title"],
                        "agent": ", ".join(row["functions"]), "profile": _profile(row["functions"], row["role"]),
                        "admission": admission.get(row["id"]), "category": row["category"], "pause_reason": row["pause_reason"],
                        "retained_continuation": row["status"] == "pending" and bool(own_threads)},
            "attempts": [{"id": item["id"], "number": item["number"], "status": _status(item["status"]),
                          "host_id": item["host_name"], "profile_id": _profile(row["functions"], row["role"]), "started_at": item["started_at"],
                          "finished_at": item["finished_at"], "error": safe_text((item["failure"] or {}).get("message")) or None}
                         for item in own_attempts]}
        if row["retry_at"]:
            session["recovery"] = {"state": "waiting_for_infrastructure", "reason": row["status_note"], "retry_at": row["retry_at"].timestamp()}
        result.append(session)
        for child in (item for item in threads_by_assignment.get(row["id"], []) if item["kind"] == "child"):
            child_requests = requests_by_thread.get(child["id"], [])
            latest = child_requests[-1] if child_requests else {}
            descriptor = next((descriptors[item["reviewer_descriptor_revision_id"]] for item in reversed(child_requests)
                               if item["reviewer_descriptor_revision_id"] in descriptors), {})
            parent_request = request_by_id.get(child["parent_request_id"], {})
            parent = thread_by_id.get(parent_request.get("provider_thread_id"), {})
            start = next((item["started_at"] for item in child_requests if item["started_at"]), None)
            finish = latest.get("finished_at")
            options = child["applied_model_options"]
            session["subagents"].append({**{key: value for key, value in session.items() if key != "subagents"},
                "id": child["id"], "revision": child["revision"], "native": True,
                "label": f"{row['number']}.{child['number']}", "session_number": None,
                "parent_session_id": parent.get("id") if parent.get("kind") == "child" else row["id"],
                "title": safe_text(descriptor.get("slug") or child.get("label") or f"Subagent {child['number']}", 256),
                "description": safe_text(descriptor.get("instructions") or child.get("description") or "", 16000),
                "descriptor_id": descriptor.get("id"), "descriptor_revision": descriptor.get("revision"),
                "role": "reviewer" if descriptor else "worker",
                "functions": [],
                "status": _status(latest.get("status", "waiting")), "created_at": child["created_at"],
                "started_at": start, "finished_at": finish, "usage": _usage(usage_rows.get(child["id"])),
                "models": [options["model"]] if options.get("model") else [],
                "efforts": [options["reasoning_effort"]] if options.get("reasoning_effort") else [],
                "skills": [], "can_resume": False, "queue_position": None, "attempt_count": 0, "attempts": [],
                "elapsed_seconds": _seconds(start, finish, now),
                "agent_seconds": sum(_seconds(item["started_at"], item["finished_at"], now) for item in child_requests),
                "context": {"provider_context": child["provider_thread_id"]}})
    return result, page["next_cursor"]


def _sessions(conn, run_id, assignment_id=None, *, service):
    return _sessions_page(conn, run_id, assignment_id, service=service)[0]


def run_detail(conn, actor, identifier, *, service, compact=False, view="full", sessions_before=None,
               sessions_limit=SESSION_PAGE_LIMIT):
    run, mission, project = (tables[key] for key in ("run", "mission", "project"))
    row = conn.execute(select(*_run_columns(run), mission.c.project_id, mission.c.title,
        project.c.title.label("project_title")).select_from(run.join(mission).join(project))
        .where(run.c.id == identifier)).mappings().first()
    if row is None:
        raise DomainError("not_found", "Run not found", 404)
    require_project(conn, actor, row["project_id"])
    if view == "queue":
        assignment = tables["assignment"]
        page_rows = _session_summary_page(conn, identifier, before=sessions_before, limit=sessions_limit)[0]
        page_ids = [row["id"] for row in page_rows if row["status"] == "queued"]
        pending = list(conn.execute(select(assignment).where(assignment.c.id.in_(page_ids))).mappings()) if page_ids else []
        values = admissions(conn, pending, service, conn.execute(select(func.now())).scalar_one())
        return {"admissions": {str(key): value for key, value in values.items()}}
    if compact:
        sessions, next_before = _session_summary_page(conn, identifier, before=sessions_before, limit=sessions_limit)
        return {**_run_views(conn, [row], compact=True)[0], "sessions": sessions,
                "sessions_next_before": next_before}
    result = _run_views(conn, [row])[0]
    if view != "metrics":
        sessions, next_before = _sessions_page(conn, identifier, service=service, before=sessions_before,
                                               limit=sessions_limit)
        result["sessions"] = [{key: value for key, value in session.items() if key != "attempts"}
            for session in sessions]
        result["sessions_next_before"] = next_before
    return result


def _resolve(conn, actor, identifier):
    assignment = conn.execute(select(tables["assignment"]).where(tables["assignment"].c.id == identifier)).mappings().first()
    native = None
    if assignment is None:
        native = get(conn, "provider_thread", identifier)
        if native["kind"] != "child":
            raise DomainError("not_found", "Session not found", 404)
        assignment = get(conn, "assignment", native["assignment_id"])
    require_project(conn, actor, project_of(conn, "assignment", assignment["id"]))
    return assignment, native


def _event(row, assignment):
    category = row.get("category")
    kind = {"message": "agent.message", "tool": "provider.tool", "usage": "provider.usage",
            "api": "api.request", "forge": "forge.change", "zulip": "zulip.message"}.get(category, row["kind"])
    data = {"source": row.get("source"), "usage": row.get("usage")}
    if category == "message":
        data["markdown"] = row.get("detail") or row.get("title") or row.get("summary") or ""
    elif row.get("detail"):
        data["detail"] = row["detail"]
    return {"id": row["id"], "kind": kind, "title": row.get("title") or row.get("summary") or kind,
            "created_at": row["occurred_at"], "sort_at": row.get("recorded_at", row.get("created_at")),
            "session_id": assignment["id"], "run_id": assignment["run_id"], "attempt_id": row.get("execution_id"),
            "links": [], "data": data}


def events(conn, actor, identifier, *, store, before=None, limit=50, compact=False):
    assignment, native = _resolve(conn, actor, identifier)
    if native:
        table = tables["activity"]
        page = readmodels.page(conn, select(table).where(table.c.provider_thread_id == identifier), table,
                               before, limit, order="occurred_at", descending=True, tie_order="created_at")
        enrich(conn, page["items"], store=store)
    else:
        page = timeline(conn, assignment["id"], before, limit, store=store)
    items = [_event(row, assignment) for row in page["items"]]
    if compact:
        for item, row in zip(items, page["items"]):
            for field in ("markdown", "detail"):
                value = item["data"].get(field)
                if isinstance(value, str) and len(value.encode()) > 1200:
                    item["data"][field] = value.encode()[:1200].decode(errors="ignore")
                    item["detail_path"] = f"/dashboard/activity/sessions/{identifier}/events/{row['id']}"
    return {"events": items, "next_before": page["next_cursor"]}


def event_detail(conn, actor, identifier, event_id, *, store):
    assignment, native = _resolve(conn, actor, identifier)
    table = tables["activity"]
    query = select(table).where(table.c.id == event_id, table.c.assignment_id == assignment["id"])
    if native:
        query = query.where(table.c.provider_thread_id == native["id"])
    row = conn.execute(query).mappings().first()
    if row is None:
        raise DomainError("not_found", "Event not found in this session", 404)
    rows = [dict(row)]
    enrich(conn, rows, store=store)
    return _event(rows[0], assignment)


def session_detail(conn, actor, identifier, *, store, service, compact=False, view="full"):
    assignment, native = _resolve(conn, actor, identifier)
    if view == "reports":
        return {"reports": _reports(conn, assignment, native)}
    parent = _sessions(conn, assignment["run_id"], assignment["id"], service=service)[0]
    session = next(row for row in parent["subagents"] if row["id"] == identifier) if native else parent
    if compact:
        return session
    session.update(events(conn, actor, identifier, store=store))
    session["reports"] = _reports(conn, assignment, native)
    if not native:
        session["context"]["goal"] = get(conn, "mission", assignment["mission_id"])["objective"]
    return session


def _reports(conn, assignment, native):
    reports = []
    identifier = assignment["id"]
    if not native:
        mission = get(conn, "mission", assignment["mission_id"])
        obligation = tables["obligation"]
        ledger = list(conn.execute(select(*[obligation.c[key] for key in ("id", "number", "description", "status", "resolution", "updated_at")],
            func.jsonb_path_query_array(obligation.c.comments, cast('$[last-4 to last]', JSONPATH)).label("comments")).where(obligation.c.assignment_id == identifier)
                                  .order_by(obligation.c.number).limit(200)).mappings())
        if ledger:
            markdown = "# Current goal ledger\n\n" + mission["objective"] + "\n\n"
            for row in ledger:
                markdown += f"- [{' ' if row['status'] == 'open' else 'x'}] **{row['status']}**: {row['description']}\n"
                resolution = row["resolution"] or {}
                if resolution.get("note"):
                    markdown += "\n  " + resolution["note"] + "\n"
            reports = [{"id": str(identifier) + ":ledger", "revision": assignment["revision"],
                "kind": "context", "label": "Current goal ledger", "markdown": safe_text(markdown, 32000),
                "created_at": max(row["updated_at"] for row in ledger),
                "ledger": {"mission": mission["objective"], "acceptance_criteria": mission["acceptance_criteria"],
                    "items": [{key: row[key] for key in ("id", "number", "description", "status", "resolution", "comments")} for row in ledger]}}]
    return reports


def logs(conn, actor, identifier, *, store, limit=50):
    page = events(conn, actor, identifier, store=store, limit=limit)
    return {"enabled": True, "records": [{"at": row["created_at"], "kind": row["kind"], "title": row["title"],
        **row["data"]} for row in page["events"]], "next_before": page["next_before"]}
