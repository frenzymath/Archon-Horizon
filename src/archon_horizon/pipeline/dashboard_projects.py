"""Bounded project directories and explicit source-document detail views."""

from pathlib import PurePosixPath
from urllib.parse import quote
from uuid import UUID

from sqlalchemy import and_, func, or_, select

from .auth import require_project
from .errors import DomainError
from .integration_views import repository_url
from .records import get
from .schema import tables
from .graph_progress import progress_view, target_context


def _source_query(kind, *, content=False):
    source, projection, repository, integration = (tables[name] for name in
        (kind, "source_projection", "repository", "integration"))
    fields = [source, projection.c.metadata, repository.c.remote_path,
        integration.c.id.label("integration_identifier"), integration.c.endpoint]
    if content:
        fields.append(projection.c.markdown)
    return select(*fields).select_from(source.join(repository, source.c.source_repository_id == repository.c.id)
        .join(integration).outerjoin(projection, and_(projection.c.repository_id == source.c.source_repository_id,
            projection.c.source_path == source.c.source_path, projection.c.source_commit_oid == source.c.source_commit_oid)))


def _view(raw, config, context=None):
    row = dict(raw)
    metadata = row.get("metadata") or {}
    row["metadata"] = metadata
    row["label"] = metadata.get("label") or metadata.get("id") or PurePosixPath(row["source_path"]).stem
    row["labels"] = metadata.get("labels", [])
    if context is not None:
        row.update(progress_view(metadata, context))
        row["metadata"] = {**metadata, "labels": row["labels"]}
        if row.get("implementation"):
            row["status"] = row["implementation"]["status"]
            row["metadata"].pop("stage", None)
    row["kind"] = metadata.get("type") or row.get("kind") or "node"
    row["lifecycle"] = "archived" if row["archived_at"] else "active"
    base = repository_url(row, {"id": row.pop("integration_identifier"), "endpoint": row.pop("endpoint")}, config)
    row["source_url"] = (f"{base}/src/commit/{quote(row['source_commit_oid'], safe='')}/"
        f"{quote(row['source_path'], safe='/')}") if base else None
    return row


def _relations(conn, nodes, context=None):
    dependency = tables["node_dependency"]
    ids = [row["id"] for row in nodes]
    children = {}
    for child, parent in conn.execute(select(dependency).where(dependency.c.child_node_id.in_(ids))):
        children.setdefault(child, []).append(parent)
    for row in nodes:
        row["children"] = children.get(row["id"], [])
    if context and context["target_repository_id"] and nodes:
        node, projection = tables["node"], tables["source_projection"]
        sources = list(conn.execute(select(node.c.id, node.c.source_repository_id, node.c.source_path,
            projection.c.metadata).join(projection, and_(node.c.source_repository_id == projection.c.repository_id,
                node.c.source_path == projection.c.source_path, node.c.source_commit_oid == projection.c.source_commit_oid))
            .where(node.c.project_id == nodes[0]["project_id"], node.c.archived_at.is_(None))).mappings())
        keys = {(item["source_repository_id"], item["metadata"].get("label") or item["metadata"].get("id")
                 or PurePosixPath(item["source_path"]).stem): item["id"] for item in sources}
        dependents = {}
        for item in sources:
            implementation = item["metadata"].get("implementations", {}).get(str(context["target_repository_id"]), {})
            for key in implementation.get("children", item["metadata"].get("children", [])):
                prerequisite = keys.get((item["source_repository_id"], key))
                if prerequisite is not None:
                    dependents.setdefault(prerequisite, []).append(item["id"])
        for row in nodes:
            row["parents"] = dependents.get(row["id"], [])
            implementation = row["metadata"].get("implementations", {}).get(str(context["target_repository_id"]), {})
            if "children" in implementation:
                row["children"] = [keys[(row["source_repository_id"], key)] for key in implementation["children"]
                                   if (row["source_repository_id"], key) in keys]
    return nodes


def overview(conn, actor, project_id):
    require_project(conn, actor, project_id)
    row = get(conn, "project", project_id)
    for name in ("node", "mission", "document"):
        table = tables[name]
        row[name + "_count"] = conn.execute(select(func.count()).select_from(table).where(
            table.c.project_id == project_id, table.c.archived_at.is_(None))).scalar_one()
    return row


def nodes(conn, actor, project_id, config, *, search="", label="", offset=0, limit=50, target_repository_id=None):
    require_project(conn, actor, project_id)
    context = target_context(conn, project_id, target_repository_id)
    node, projection = tables["node"], tables["source_projection"]
    query = _source_query("node").where(node.c.project_id == project_id, node.c.archived_at.is_(None))
    if search:
        from .readmodels import _pattern
        query = query.where(or_(node.c.title.ilike(_pattern(search), escape="\\"),
            node.c.source_path.ilike(_pattern(search), escape="\\")))
    query = query.where(projection.c.source_path.is_not(None))
    if label:
        rows = [_view(row, config, context) for row in conn.execute(query.order_by(node.c.number).limit(10001)).mappings()]
        if len(rows) > 10000:
            raise DomainError("graph_too_large", "Narrow the node search before filtering progress", 422)
        rows = [row for row in rows if label in row["labels"]]
        return {**context, "nodes": rows[offset:offset + limit], "total": len(rows), "offset": offset, "limit": limit}
    total = conn.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    rows = [_view(row, config, context) for row in conn.execute(query.order_by(node.c.number).offset(offset).limit(limit)).mappings()]
    return {**context, "nodes": rows, "total": total, "offset": offset, "limit": limit}


def node_detail(conn, actor, project_id, identifier, config, target_repository_id=None):
    require_project(conn, actor, project_id)
    context = target_context(conn, project_id, target_repository_id)
    node, projection = tables["node"], tables["source_projection"]
    try:
        match = node.c.id == UUID(identifier)
    except ValueError:
        match = or_(projection.c.metadata["label"].astext == identifier,
            projection.c.metadata["id"].astext == identifier,
            func.regexp_replace(node.c.source_path, r"^.*/|\.md$", "", "g") == identifier)
    rows = list(conn.execute(_source_query("node", content=True).where(node.c.project_id == project_id,
        node.c.archived_at.is_(None), match).limit(2)).mappings())
    if len(rows) != 1:
        raise DomainError("node_not_found", "Node was not found uniquely in this project", 404)
    row = _relations(conn, [_view(rows[0], config, context)], context)[0]
    dependency = tables["node_dependency"]
    adjacent_ids = set(row["children"]) | set(row.get("parents", []))
    if "parents" not in row:
        adjacent_ids.update(conn.execute(select(dependency.c.child_node_id)
            .where(dependency.c.parent_node_id == row["id"])).scalars())
    adjacent = [_view(item, config, context) for item in conn.execute(_source_query("node").where(
        node.c.project_id == project_id, node.c.id.in_(adjacent_ids))).mappings()]
    return {**context, "node": row, "nodes": [row, *adjacent], "project_id": project_id}


def node_summaries(conn, actor, project_id, identifiers, config, target_repository_id=None):
    require_project(conn, actor, project_id)
    context = target_context(conn, project_id, target_repository_id)
    node, projection = tables["node"], tables["source_projection"]
    if len(identifiers) > 100:
        raise DomainError("invalid_limit", "At most 100 node references may be resolved", 422)
    uuid_ids = []
    for identifier in identifiers:
        try:
            uuid_ids.append(UUID(identifier))
        except ValueError:
            pass
    rows = conn.execute(_source_query("node").where(node.c.project_id == project_id, node.c.archived_at.is_(None),
        or_(node.c.id.in_(uuid_ids), projection.c.metadata["label"].astext.in_(identifiers),
            projection.c.metadata["id"].astext.in_(identifiers),
            func.regexp_replace(node.c.source_path, r"^.*/|\.md$", "", "g").in_(identifiers))).limit(100)).mappings()
    return {**context, "nodes": [_view(row, config, context) for row in rows]}


def objectives(conn, actor, project_id, config, identifier=None):
    require_project(conn, actor, project_id)
    document = tables["document"]
    query = _source_query("document", content=identifier is not None).where(
        document.c.project_id == project_id, document.c.kind == "roadmap", document.c.archived_at.is_(None))
    if identifier:
        query = query.where(document.c.id == identifier)
    rows = [_view(row, config) for row in conn.execute(query.order_by(document.c.number).limit(100)).mappings()]
    if identifier and not rows:
        raise DomainError("objective_not_found", "Objective was not found in this project", 404)
    return rows[0] if identifier else {"items": rows}


def graph(conn, actor, project_id, config, focus=None, target_repository_id=None):
    require_project(conn, actor, project_id)
    context = target_context(conn, project_id, target_repository_id)
    node, projection = tables["node"], tables["source_projection"]
    rows = _relations(conn, [_view(row, config, context) for row in conn.execute(_source_query("node").where(
        node.c.project_id == project_id, node.c.archived_at.is_(None), projection.c.source_path.is_not(None))
        .order_by(node.c.number).limit(10001)).mappings()], context)
    if len(rows) > 10000:
        raise DomainError("graph_too_large", "Graph exceeds the interactive view limit", 422)
    if focus:
        by_id = {str(row["id"]): row for row in rows}
        if focus not in by_id:
            raise DomainError("node_not_found", "Node was not found in this project", 404)
        pending, seen = [focus], set()
        while pending:
            current = pending.pop()
            if current in seen or current not in by_id:
                continue
            seen.add(current)
            pending.extend(str(identifier) for identifier in by_id[current]["children"])
        rows = [row for row in rows if str(row["id"]) in seen]
    return {**context, "project_id": project_id, "scope": "node" if focus else "project", "nodes": rows}


def missions(conn, actor, project_id, *, offset=0, limit=50, search="", status=""):
    require_project(conn, actor, project_id)
    mission, run = tables["mission"], tables["run"]
    query = select(mission).where(mission.c.project_id == project_id, mission.c.archived_at.is_(None))
    if search:
        from .readmodels import _pattern
        query = query.where(or_(mission.c.title.ilike(_pattern(search), escape="\\"),
            mission.c.objective.ilike(_pattern(search), escape="\\")))
    if status:
        query = query.where(mission.c.status == status)
    total = conn.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    matches = query.with_only_columns(mission.c.id).order_by(mission.c.number).offset(offset).limit(limit)
    match_ids = list(conn.execute(matches).scalars())
    ancestry = select(mission.c.id, mission.c.parent_id).where(mission.c.id.in_(match_ids)).cte("mission_page_ancestors", recursive=True)
    ancestry = ancestry.union(select(mission.c.id, mission.c.parent_id).join(ancestry, mission.c.id == ancestry.c.parent_id)
        .where(mission.c.project_id == project_id))
    directory_fields = [column for column in mission.c if column.name not in {"objective", "closure_note"}]
    rows = [dict(row) for row in conn.execute(select(*directory_fields).where(mission.c.id.in_(select(ancestry.c.id)))
        .order_by(mission.c.number).limit(1001)).mappings()]
    if len(rows) > 1000:
        raise DomainError("mission_hierarchy_too_deep", "This page includes more than 1,000 ancestor missions; narrow the selection", 422)
    ids = [row["id"] for row in rows]
    child_counts = dict(conn.execute(select(mission.c.parent_id, func.count()).where(
        mission.c.parent_id.in_(ids), mission.c.archived_at.is_(None)).group_by(mission.c.parent_id)).all())
    latest = {row["mission_id"]: dict(row) for row in conn.execute(select(run).where(run.c.mission_id.in_(ids))
        .distinct(run.c.mission_id).order_by(run.c.mission_id, run.c.number.desc())).mappings()}
    for row in rows:
        row["latest_run"] = latest.get(row["id"])
        row["child_count"] = child_counts.get(row["id"], 0)
    return {"items": rows, "match_ids": match_ids, "total": total, "offset": offset, "limit": limit,
        "next_offset": offset + limit if offset + limit < total else None}


def mission_detail(conn, actor, project_id, identifier):
    require_project(conn, actor, project_id)
    row = get(conn, "mission", identifier)
    if row["project_id"] != project_id:
        raise DomainError("mission_not_found", "Mission was not found in this project", 404)
    row["parent_title"] = get(conn, "mission", row["parent_id"])["title"] if row["parent_id"] else None
    return row
