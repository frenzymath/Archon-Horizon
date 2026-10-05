"""Paginated, authorization-scoped dashboard projections."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
import base64
import json
import re
import shutil
from urllib.parse import quote
from uuid import UUID

from sqlalchemy import and_, cast, func, or_, select, tuple_, BigInteger

from .auth import is_admin, require_admin, require_project
from .errors import DomainError
from .records import get, json_value, project_of
from .schema import tables


def replay_window(conn, retention_seconds):
    event = tables["event"]
    cutoff = conn.execute(select(func.now())).scalar_one() - timedelta(seconds=retention_seconds)
    earliest, latest = conn.execute(select(func.min(event.c.sequence).filter(event.c.created_at >= cutoff),
                                          func.max(event.c.sequence))).one()
    return (earliest - 1 if earliest is not None else latest or 0), latest or 0


def page(conn, query, table, cursor=None, limit=50, *, order="id", descending=False, tie_order=None):
    if not 1 <= limit <= 100:
        raise DomainError("invalid_limit", "Page size must be between 1 and 100", 422)
    column = table.c[order]
    if cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            last_id = UUID(decoded["id"])
            value = last_id if order == "id" else decoded["value"]
            if column.type.python_type is datetime:
                value = datetime.fromisoformat(value)
                if value.tzinfo is None:
                    raise ValueError("timestamp cursor must have a timezone")
            elif column.type.python_type is int and type(value) is not int:
                raise ValueError("invalid numeric cursor")
            if tie_order:
                tie_value = decoded["tie_value"]
                if table.c[tie_order].type.python_type is datetime:
                    tie_value = datetime.fromisoformat(tie_value)
                    if tie_value.tzinfo is None:
                        raise ValueError("timestamp cursor must have a timezone")
                left, right = tuple_(column, table.c[tie_order], table.c.id), tuple_(value, tie_value, last_id)
                query = query.where(left < right if descending else left > right)
            else:
                query = query.where(or_(column < value, and_(column == value, table.c.id < last_id)) if descending
                                    else or_(column > value, and_(column == value, table.c.id > last_id)))
        except (ValueError, KeyError, TypeError):
            raise DomainError("invalid_cursor", "Invalid page cursor", 422) from None
    columns = (column, table.c[tie_order], table.c.id) if tie_order else (column, table.c.id)
    ordering = tuple(item.desc() for item in columns) if descending else columns
    rows = [dict(row) for row in conn.execute(query.order_by(*ordering).limit(limit + 1)).mappings()]
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        checkpoint = {"id": str(rows[-1]["id"]), "value": json_value(rows[-1][order])}
        if tie_order:
            checkpoint["tie_value"] = json_value(rows[-1][tie_order])
        next_cursor = base64.urlsafe_b64encode(json.dumps(checkpoint).encode()).decode().rstrip("=")
    return {"items": rows, "next_cursor": next_cursor}


def visible_projects(conn, actor):
    project, grant = tables["project"], tables["project_grant"]
    query = select(project)
    if not is_admin(conn, actor):
        if actor.kind == "agent":
            from .auth import live_execution
            query = query.where(project.c.id == live_execution(conn, actor)["project_id"])
        else:
            query = query.join(grant).where(grant.c.principal_id == actor.id)
    return query


def runs(conn, actor, project_id, cursor, limit):
    require_project(conn, actor, project_id)
    run, mission = tables["run"], tables["mission"]
    query = select(run, mission.c.project_id, mission.c.title).join(mission).where(mission.c.project_id == project_id)
    return page(conn, query, run, cursor, limit, order="number")


def run_detail(conn, actor, identifier):
    row = get(conn, "run", identifier)
    mission = get(conn, "mission", row["mission_id"])
    require_project(conn, actor, mission["project_id"])
    from .admission import reason, run_usage
    usage = run_usage(conn, row)
    admission_note = reason(row, usage, conn.execute(select(func.now())).scalar_one())
    return {**row, "project_id": mission["project_id"], "title": mission["title"],
            "admission_note": admission_note, "admission_usage": usage}


def assignment_query():
    assignment, run, mission = (tables[name] for name in ("assignment", "run", "mission"))
    return select(assignment, run.c.number.label("run_number"), mission.c.title).select_from(
        assignment.join(run, assignment.c.run_id == run.c.id).join(mission, assignment.c.mission_id == mission.c.id))


def enrich_assignments(conn, rows, service):
    if not rows:
        return rows
    activity, execution, host = (tables[name] for name in ("activity", "execution", "host"))
    identifiers = [row["id"] for row in rows]
    recent = {row["assignment_id"]: row for row in conn.execute(select(activity.c.assignment_id, activity.c.summary, activity.c.occurred_at)
        .where(activity.c.assignment_id.in_(identifiers), activity.c.summary.is_not(None),
               activity.c.kind.in_(("checkpoint", "progress", "tool_use", "completion", "failure")))
        .distinct(activity.c.assignment_id)
        .order_by(activity.c.assignment_id, activity.c.created_at.desc())).mappings()}
    hosts = {row["assignment_id"]: row["display_name"] for row in conn.execute(select(execution.c.assignment_id, host.c.display_name)
        .join(host).where(execution.c.assignment_id.in_(identifiers)).distinct(execution.c.assignment_id)
        .order_by(execution.c.assignment_id, execution.c.created_at.desc())).mappings()}
    now = conn.execute(select(func.now())).scalar_one()
    observations = {}
    from .scheduler import Scheduler
    scheduler = Scheduler(service)
    observations.update(scheduler.admission_observations(conn, rows))
    for row in rows:
        state = service.readiness(conn, row, now, observations=observations)
        waiting = None
        if row["status"] == "pending":
            waiting = state.reason if not state.ready else scheduler.admission_blocker(conn, row, observations=observations)
        row.update(why_waiting=waiting,
                   last_activity=recent[row["id"]]["summary"] if row["id"] in recent else None,
                   last_activity_at=recent[row["id"]]["occurred_at"] if row["id"] in recent else None,
                   host_name=hosts.get(row["id"]))
    return rows


def assignments(conn, actor, service, run_id, cursor, limit, q="", *, status=None):
    require_project(conn, actor, project_of(conn, "run", run_id))
    table = tables["assignment"]
    query = assignment_query().where(table.c.run_id == run_id)
    if status:
        groups = {"history": ("completed", "failed", "cancelled"),
                  "running": ("running", "stopping")}
        if status not in {"history", "pending", "running", "stopping", "completed", "failed", "cancelled"}:
            raise DomainError("invalid_status", "Unknown assignment status filter", 422)
        query = query.where(table.c.status.in_(groups.get(status, (status,))))
    if q:
        reference = re.fullmatch(r"(?:R(\d+)/)?A(\d+)", q.strip(), re.IGNORECASE)
        if reference:
            query = query.where(table.c.number == int(reference[2]))
            if reference[1]:
                query = query.where(tables["run"].c.number == int(reference[1]))
        else:
            needle = _pattern(q)
            node, link = tables["node"], tables["mission_node"]
            scope_match = select(link.c.mission_id).join(node).where(link.c.mission_id == table.c.mission_id,
                or_(node.c.title.ilike(needle, escape="\\"), node.c.source_path.ilike(needle, escape="\\"))).exists()
            query = query.where(or_(tables["mission"].c.title.ilike(needle, escape="\\"), table.c.status == q[:30], scope_match))
    result = page(conn, query, table, cursor, limit, order="queue_rank")
    enrich_assignments(conn, result["items"], service)
    return result


def assignment_detail(conn, actor, service, identifier):
    context = service.context(conn, actor, identifier, view="full")
    row = dict(conn.execute(assignment_query().where(tables["assignment"].c.id == identifier)).mappings().one())
    enrich_assignments(conn, [row], service)
    publications = tables["publication"]
    result = {**row, "mission": context["mission"], "obligations": context["obligations"],
              "executions": context["executions"], "provider_threads": context["provider_threads"],
              "activity": context["activity"], "publications": [],
              "baseline": context["run"]["adopted_roadmap_snapshot_id"]}
    publication_rows = [dict(row) for row in conn.execute(select(publications).where(publications.c.requested_by_assignment_id == identifier)
            .order_by(publications.c.created_at.desc()).limit(100)).mappings()]
    result["publications"] = _publication_views(conn, publication_rows, config=service.config)
    from .activity_readmodel import enrich
    enrich(conn, result["activity"], store=service.store)
    return result


def assignment_activity(conn, actor, identifier, cursor, limit, *, store=None):
    require_project(conn, actor, project_of(conn, "assignment", identifier))
    from .activity_readmodel import timeline
    return timeline(conn, identifier, cursor, limit, store=store)


def _pattern(value):
    return "%" + value[:160].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def roadmap(conn, actor, project_id, cursor, limit, run_id=None, q="", config=None, target_repository_id=None):
    require_project(conn, actor, project_id)
    from .graph_progress import progress_view, target_context
    from .dashboard_projects import _relations
    context = target_context(conn, project_id, target_repository_id, run_id)
    node, dependency = tables["node"], tables["node_dependency"]
    query = select(node).where(node.c.project_id == project_id, node.c.archived_at.is_(None))
    projection = tables["source_projection"]
    query = query.where(select(projection.c.source_path).where(
        projection.c.repository_id == node.c.source_repository_id, projection.c.source_path == node.c.source_path).exists())
    if q:
        node_ref = re.fullmatch(r"N(\d+)", q.strip(), re.IGNORECASE)
        query = query.where(node.c.number == int(node_ref[1])) if node_ref else query.where(or_(
            node.c.title.ilike(_pattern(q), escape="\\"), node.c.source_path.ilike(_pattern(q), escape="\\")))
    result = page(conn, query, node, cursor, limit, order="number")
    parents = {}
    if result["items"]:
        for edge in conn.execute(select(dependency).where(dependency.c.child_node_id.in_([row["id"] for row in result["items"]]))).mappings():
            parents.setdefault(edge["child_node_id"], []).append(edge["parent_node_id"])
    from .integration_views import repository_url
    projection = tables["source_projection"]
    details = {(row["repository_id"], row["source_path"]): row for row in conn.execute(
        select(projection).join(node, and_(node.c.source_repository_id == projection.c.repository_id,
            node.c.source_path == projection.c.source_path, node.c.source_commit_oid == projection.c.source_commit_oid))
        .where(node.c.id.in_([row["id"] for row in result["items"]]))).mappings()}
    dependency_ids = {identifier for ids in parents.values() for identifier in ids}
    numbers = dict(conn.execute(select(node.c.id, node.c.number).where(node.c.id.in_(dependency_ids))).all())
    for row in result["items"]:
        detail = details.get((row["source_repository_id"], row["source_path"]))
        metadata = detail["metadata"] if detail else {}
        progress = progress_view(metadata, context)
        labels = progress["labels"]
        status = ("conditionally_proved" if "conditionally_proved" in labels else next((label for label in (
            "formally_proved", "formally_stated", "informal_proved", "proof_sketch", "informal_stated") if label in labels),
            "indexed" if detail else "not_indexed"))
        row.update(status=status, dependencies=parents.get(row["id"], []), labels=labels,
            dependency_numbers=[numbers[identifier] for identifier in parents.get(row["id"], [])],
            source_key=metadata.get("label") or metadata.get("id") or PurePosixPath(row["source_path"]).stem, kind=metadata.get("type"),
            content=detail["markdown"] if detail else None)
        row.update(progress)
        row["metadata"] = metadata
        if progress["implementation"]:
            row["status"] = progress["implementation"]["status"] if detail else "not_indexed"
    _relations(conn, result["items"], context)
    for row in result["items"]:
        row["dependencies"] = row["children"]
        row.pop("metadata", None)
    all_ids = {identifier for row in result["items"] for identifier in row["dependencies"]}
    numbers = dict(conn.execute(select(node.c.id, node.c.number).where(node.c.id.in_(all_ids))).all())
    for row in result["items"]:
        row["dependency_numbers"] = [numbers[identifier] for identifier in row["dependencies"] if identifier in numbers]
    result.update(context)
    repository, integration = tables["repository"], tables["integration"]
    repository_ids = {row["source_repository_id"] for row in result["items"]}
    sources = {row["id"]: dict(row) for row in conn.execute(select(repository, integration.c.endpoint,
        integration.c.id.label("integration_identifier"))
        .join(integration).where(repository.c.id.in_(repository_ids))).mappings()} if repository_ids else {}
    for row in result["items"]:
        source = sources.get(row["source_repository_id"])
        base = repository_url(source, {"id": source["integration_identifier"], "endpoint": source["endpoint"]}, config) if source else None
        row["source_url"] = f"{base}/src/commit/{quote(row['source_commit_oid'], safe='')}/{quote(row['source_path'], safe='/')}" if base else None
    snapshot = tables["roadmap_snapshot"]
    baseline = None
    if run_id:
        run = run_detail(conn, actor, run_id)
        if run["project_id"] != project_id:
            raise DomainError("scope_mismatch", "Selected run belongs to another project", 422)
        if run["adopted_roadmap_snapshot_id"]:
            baseline = get(conn, "roadmap_snapshot", run["adopted_roadmap_snapshot_id"])
    result["baseline"] = dict(baseline) if baseline else None
    document = tables["document"]
    result["documents"] = []
    for raw in conn.execute(select(document, projection.c.markdown, projection.c.indexed_at,
            repository.c.remote_path, integration.c.endpoint, integration.c.id.label("integration_identifier"))
            .join(repository, document.c.source_repository_id == repository.c.id).join(integration)
            .outerjoin(projection, and_(projection.c.repository_id == document.c.source_repository_id,
                projection.c.source_path == document.c.source_path,
                projection.c.source_commit_oid == document.c.source_commit_oid))
            .where(document.c.project_id == project_id, document.c.kind == "roadmap", document.c.archived_at.is_(None))
            .order_by(document.c.number).limit(100)).mappings():
        row = dict(raw)
        base = repository_url(row, {"id": row.pop("integration_identifier"), "endpoint": row.pop("endpoint")}, config)
        row["source_url"] = f"{base}/src/commit/{quote(row['source_commit_oid'], safe='')}/{quote(row['source_path'], safe='/')}" if base else None
        row["content"] = row.pop("markdown")
        result["documents"].append(row)
    result["summary"] = {
        "node_count": conn.execute(select(func.count()).select_from(node).where(node.c.project_id == project_id,
            node.c.archived_at.is_(None))).scalar_one(),
        "dependency_count": conn.execute(select(func.count()).select_from(dependency.join(node,
            dependency.c.child_node_id == node.c.id)).where(node.c.project_id == project_id,
            node.c.archived_at.is_(None))).scalar_one(),
        "indexed_at": conn.execute(select(func.max(projection.c.indexed_at)).select_from(projection.join(repository))
            .where(repository.c.project_id == project_id)).scalar_one(),
    }
    return result


def publication_view(conn, row, *, artifacts=None, repositories=None, config=None):
    artifact = artifacts[row["artifact_id"]] if artifacts is not None else get(conn, "artifact", row["artifact_id"])
    repository_id = artifact["content"].get("repository_id")
    repository = (repositories.get(UUID(repository_id)) if repositories is not None else get(conn, "repository", repository_id)) if repository_id else None
    from .integration_views import repository_url
    base = None
    if repository:
        integration = ({"id": repository["integration_id"], "endpoint": repository["endpoint"]}
            if "endpoint" in repository else get(conn, "integration", repository["integration_id"]))
        base = repository_url(repository, integration, config)
    commit = artifact["content"].get("commit_oid")
    return {**row, "title": row["target"].get("ref_name", "Artifact preservation"),
            "url": f"{base}/commit/{quote(commit, safe='')}" if base and commit else base,
            "repository": repository["slug"] if repository else "Artifact store", "kind": "publication",
            "worker_managed": row["target"].get("ref_name", "").startswith(("refs/horizon/", "refs/heads/horizon/recovery/")),
            "commit_oid": artifact["content"].get("commit_oid"),
            "failure": row["failure"].get("message") if row["failure"] else None}


def _publication_views(conn, rows, config=None):
    if not rows:
        return []
    artifact, repository = tables["artifact"], tables["repository"]
    artifacts = {row["id"]: row for row in conn.execute(select(artifact).where(
        artifact.c.id.in_([row["artifact_id"] for row in rows]))).mappings()}
    repository_ids = {UUID(row["content"]["repository_id"]) for row in artifacts.values() if row["content"].get("repository_id")}
    integration = tables["integration"]
    repositories = {row["id"]: row for row in conn.execute(select(repository, integration.c.endpoint).join(integration)
        .where(repository.c.id.in_(repository_ids))).mappings()} if repository_ids else {}
    return [publication_view(conn, row, artifacts=artifacts, repositories=repositories, config=config) for row in rows]


def changes(conn, actor, project_id, cursor, limit, q="", config=None):
    require_project(conn, actor, project_id)
    publication, artifact = tables["publication"], tables["artifact"]
    query = select(publication).join(artifact).where(artifact.c.project_id == project_id)
    if q:
        query = query.where(or_(publication.c.target["ref_name"].astext.ilike(_pattern(q), escape="\\"), publication.c.status == q))
    result = page(conn, query, publication, cursor, limit)
    result["items"] = _publication_views(conn, result["items"], config=config)
    unsettled = publication.c.status.in_(("pending", "running", "failed"))
    result["preservation"] = dict(conn.execute(select(
        func.count().filter(unsettled).label("pending_count"),
        func.count().filter(publication.c.status == "failed").label("blocked_count"),
        func.min(publication.c.created_at).filter(unsettled).label("oldest_pending_at"),
        func.max(publication.c.verified_at).filter(publication.c.status == "verified").label("last_verified_at"),
    ).select_from(publication.join(artifact)).where(artifact.c.project_id == project_id,
        publication.c.target["kind"].astext == "git",
        or_(publication.c.target["ref_name"].astext.startswith("refs/horizon/"),
            publication.c.target["ref_name"].astext.startswith("refs/heads/horizon/recovery/")))).mappings().one())
    return result


def forge_items(conn, actor, project_id, cursor, limit, q="", config=None):
    require_project(conn, actor, project_id)
    forge, repository, integration, gate = (tables[name] for name in ("forge_item", "repository", "integration", "review_gate"))
    query = select(forge, repository.c.slug.label("repository"), repository.c.remote_path,
                   integration.c.endpoint, integration.c.id.label("integration_identifier")
                   ).select_from(forge.join(repository).join(integration)).where(repository.c.project_id == project_id)
    if q:
        query = query.where(or_(forge.c.title.ilike(_pattern(q), escape="\\"), forge.c.status == q))
    result = page(conn, query, forge, cursor, limit, order="updated_at", descending=True)
    identifiers = [row["id"] for row in result["items"]]
    gates = {row["forge_item_id"]: row for row in conn.execute(select(gate).where(gate.c.forge_item_id.in_(identifiers))).mappings()} if identifiers else {}
    for row in result["items"]:
        from .integration_views import repository_url
        base = repository_url(row, {"id": row.pop("integration_identifier"), "endpoint": row.pop("endpoint")}, config)
        row["url"] = f"{base}/{'pulls' if row['kind'] == 'pull_request' else 'issues'}/{row['remote_number']}" if base else None
        row["reviewed_head"] = gates[row["id"]]["accepted_commit_oid"] if row["id"] in gates else None
        row["review_status"] = gates[row["id"]]["status"] if row["id"] in gates else None
        if row["kind"] == "pull_request":
            from .reviews import review_readiness
            row["review_readiness"] = review_readiness(conn, row)
    return result


def discussions(conn, actor, project_id, cursor, limit, assignment_id=None, q="", config=None):
    require_project(conn, actor, project_id)
    if assignment_id:
        if project_of(conn, "assignment", assignment_id) != project_id:
            raise DomainError("scope_mismatch", "Selected assignment belongs to another project", 422)
    discussion, message, subscription, reference = (tables[name] for name in (
        "discussion", "message", "subscription", "object_reference"))
    integration = tables["integration"]
    query = select(discussion, integration.c.endpoint).join(integration).where(discussion.c.project_id == project_id)
    if q:
        query = query.where(discussion.c.topic.ilike(_pattern(q), escape="\\"))
    result = page(conn, query, discussion, cursor, limit, order="updated_at", descending=True)
    identifiers = [row["id"] for row in result["items"]]
    subscriptions, unread = {}, {}
    if assignment_id and identifiers:
        subscriptions = {row["discussion_id"]: row["mode"] for row in conn.execute(
            select(reference.c.discussion_id, subscription.c.mode).select_from(subscription.join(reference)).where(
                subscription.c.assignment_id == assignment_id, reference.c.kind == "discussion",
                reference.c.discussion_id.in_(identifiers))).mappings()}
        receipt = tables["message_read"]
        unread = dict(conn.execute(select(message.c.discussion_id, func.count()).where(
            message.c.discussion_id.in_(identifiers), message.c.deleted_at.is_(None),
            ~select(receipt.c.message_id).where(receipt.c.assignment_id == assignment_id,
                receipt.c.message_id == message.c.id, receipt.c.message_revision == message.c.revision).exists()
            ).group_by(message.c.discussion_id)).all())
    for row in result["items"]:
        from .integration_views import public_url
        base = public_url({"id": row["integration_id"], "endpoint": row.pop("endpoint")}, config)
        row["url"] = (base + "/#narrow/stream/" + quote(row["channel_remote_id"], safe="") + "/topic/" +
            quote(row["topic"], safe="")) if base else None
        row.update(title=row["topic"], subscribed=subscriptions.get(row["id"]) not in (None, "muted"), unread_count=unread.get(row["id"], 0))
    return result


def discussion_messages(conn, actor, discussion_id, cursor, limit, assignment_id=None, *, unread_only=False):
    discussion = get(conn, "discussion", discussion_id)
    require_project(conn, actor, discussion["project_id"])
    if assignment_id and project_of(conn, "assignment", assignment_id) != discussion["project_id"]:
        raise DomainError("scope_mismatch", "Selected assignment belongs to another project", 422)
    from .communications import discussion_fingerprint
    fingerprint = discussion_fingerprint(conn, discussion_id)
    message = tables["message"]
    query = select(message).where(message.c.discussion_id == discussion_id)
    if unread_only:
        if not assignment_id:
            raise DomainError("missing_assignment", "Unread discussion views require an assignment", 422)
        from .communications import discussion_read_scope
        scope, known, _ = discussion_read_scope(conn, discussion_id, assignment_id)
        query = query.where(scope, ~known)
    else:
        query = query.where(message.c.deleted_at.is_(None))
    result = page(conn, query, message, cursor, min(limit, 20), order="posted_at", descending=True)
    result["items"] = [{**row, "author": row["remote_author_id"], "content": row["body"], "created_at": row["posted_at"]}
                       for row in result["items"]]
    result["fingerprint"] = fingerprint
    if unread_only:
        result["unread_count"] = conn.execute(select(func.count()).select_from(message).where(scope, ~known)).scalar_one()
        result["older_history_available"] = conn.execute(select(message.c.id).where(
            message.c.discussion_id == discussion_id, ~scope).limit(1)).first() is not None
        result["history_url"] = f"/api/v3/discussions/{discussion_id}/messages"
        result["read_revisions"] = [{"id": row["id"], "revision": row["revision"]} for row in result["items"]]
        result["sync_status"] = discussion["sync_status"]
        # Receipts acknowledge only exact fetched revisions, never resolve work.
        result["obligations_url"] = f"/api/v3/records/obligation?assignment_id={assignment_id}&status=open"
    return result


def resources(conn, actor, config):
    require_admin(conn, actor)
    host, hh, execution, harness = (tables[name] for name in ("host", "host_harness", "execution", "harness"))
    now = conn.execute(select(func.now())).scalar_one()
    hosts = []
    slot_counts = select(hh.c.host_id, func.sum(hh.c.execution_slots).label("slots"),
        func.sum(hh.c.max_parallel_subagents).label("child_slots")).join(harness).where(
            hh.c.enabled.is_(True), harness.c.enabled.is_(True)).group_by(hh.c.host_id).subquery()
    busy_counts = select(execution.c.host_id, func.count().label("busy")).where(
        execution.c.status.in_(("starting", "running", "stopping")), execution.c.lease_expires_at > now).group_by(execution.c.host_id).subquery()
    query = select(host, func.coalesce(slot_counts.c.slots, 0).label("slots"),
        func.coalesce(slot_counts.c.child_slots, 0).label("child_slots"), func.coalesce(busy_counts.c.busy, 0).label("busy")
        ).outerjoin(slot_counts, host.c.id == slot_counts.c.host_id).outerjoin(busy_counts, host.c.id == busy_counts.c.host_id)
    for row in conn.execute(query.order_by(host.c.display_name)).mappings():
        recent = row["heartbeat_at"] and (now - row["heartbeat_at"]).total_seconds() < config.host_stale_seconds
        health = row.get("health") or {}
        storage_blocked = health.get("status") == "storage_pressure"
        detail = (f"Storage blocked: {health.get('free_bytes', 0) / 1024**3:.1f} GiB free; "
                  f"{health.get('required_free_bytes', 0) / 1024**3:.1f} GiB required") if storage_blocked else None
        status = "storage_pressure" if recent and storage_blocked and row["mode"] == "enabled" else row["mode"] if recent else "unavailable"
        hosts.append({"id": row["id"], "name": row["display_name"], "status": status, "detail": detail,
                      "slots": row["slots"], "occupied_slots": row["busy"], "heartbeat_at": row["heartbeat_at"],
                      "child_slots": row["child_slots"], "sandbox": row["sandbox"]})
    artifact = tables["artifact"]
    protected = conn.execute(select(func.coalesce(func.sum(cast(artifact.c.content["size_bytes"].astext, BigInteger)), 0))
        .where(artifact.c.kind == "blob")).scalar_one()
    outbox = tables["outbox_operation"]
    oldest = conn.execute(select(func.min(outbox.c.created_at)).where(outbox.c.status.in_(("pending", "running", "uncertain")))).scalar_one()
    limits, claim = tables["resource_limit"], tables["resource_claim"]
    active_claims = select(claim.c.resource_limit_id, func.sum(claim.c.units).label("occupied")).join(execution).where(
        claim.c.released_at.is_(None), execution.c.lease_expires_at > now,
        execution.c.status.in_(("starting", "running", "stopping"))).group_by(claim.c.resource_limit_id).subquery()
    capacity = [dict(row) for row in conn.execute(select(limits, func.coalesce(active_claims.c.occupied, 0).label("occupied"))
        .outerjoin(active_claims, limits.c.id == active_claims.c.resource_limit_id)).mappings()]
    cooling = sum(bool(row["cooldown_until"] and row["cooldown_until"] > now) for row in capacity if row["kind"] == "provider_account")
    try:
        free = shutil.disk_usage(config.state_root).free
    except OSError:
        free = None
    return {"hosts": hosts, "storage": [{"category": "Durable evidence", "bytes": protected,
            "protected_bytes": protected, "reclaimable_bytes": 0}], "observed_at": now, "free_bytes": free,
            "oldest_pending_delivery": oldest, "backup_status": "No backup evidence recorded",
            "provider_status": f"{cooling} account(s) cooling down" if capacity else "No account capacity observations",
            "limits": capacity}
