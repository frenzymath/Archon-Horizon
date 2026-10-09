"""Validated catalog creation; collection references become real relations."""

from pathlib import PurePosixPath
from uuid import UUID

from pydantic import Field, StrictInt
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import JSONB

from ..auth import require_admin, require_project
from ..errors import DomainError
from ..models import CREATE_MODELS, RELATIONS, Contract
from ..persistence.records import REVISIONED, change, create, emit, get, json_value, next_number, project_of, same_project, snapshot
from ..persistence.schema import REFERENCE_KINDS, tables

OPERATOR_KINDS = {"integration", "host", "harness", "host_harness", "resource_limit", "integration_identity"}
MAINTAINER_KINDS = {"repository", "review_policy", "reviewer_descriptor", "roadmap_snapshot", "workspace"}

# Identity, ownership and immutable provenance are changed through lifecycle commands.
UPDATE_FIELDS = {
    "project": {"title", "description", "workflow"},
    "integration": {"endpoint", "credential_ref", "enabled"},
    "repository": {"default_branch", "remote_path"},
    "document": {"title", "source_repository_id", "source_path", "source_commit_oid"},
    "node": {"title", "source_repository_id", "source_path", "source_commit_oid", "parent_node_ids"},
    "reference": set(CREATE_MODELS["reference"].model_fields) - {"project_id", "cite_key"},
    "review_policy": set(CREATE_MODELS["review_policy"].model_fields) - {"project_id", "slug"},
    "reviewer_descriptor": set(CREATE_MODELS["reviewer_descriptor"].model_fields) - {"project_id", "slug"},
    "host": {"display_name", "mode", "sandbox"},
    "harness": {"adapter_version", "provider_version", "model_options", "settings", "enabled"},
    "workspace": {"branch_name", "head_commit_oid", "status"},
    "resource_limit": {"max_concurrent"},
    "integration_identity": {"credential_ref", "enabled"},
}


class CatalogUpdate(Contract):
    expected_revision: StrictInt = Field(ge=1)
    changes: dict = Field(min_length=1)


def _claim_attached_limits(conn, host_id, harness_id, limits):
    from ..review.invocations import ACTIVE

    execution, request, thread, claim = (tables[name] for name in
        ("execution", "provider_request", "provider_thread", "resource_claim"))
    executions = list(conn.execute(select(execution.c.id).where(
        execution.c.host_id == host_id, execution.c.harness_id == harness_id,
        execution.c.stop_confirmed_at.is_(None)).order_by(execution.c.id).with_for_update()).scalars())
    if not executions:
        return
    children = list(conn.execute(select(request.c.id, request.c.execution_id).join(thread,
        request.c.provider_thread_id == thread.c.id).where(request.c.execution_id.in_(executions),
        thread.c.kind == "child", request.c.status.in_(ACTIVE)).order_by(request.c.id)
        .with_for_update(of=request)))
    existing = set(conn.execute(select(claim.c.resource_limit_id, claim.c.execution_id, claim.c.provider_request_id)
        .where(claim.c.resource_limit_id.in_([limit["id"] for limit in limits]),
               claim.c.execution_id.in_(executions), claim.c.released_at.is_(None))))
    for limit in limits:
        reservations = [(execution_id, None) for execution_id in executions]
        if limit["kind"] == "provider_account":
            reservations.extend((child.execution_id, child.id) for child in children)
        for execution_id, request_id in reservations:
            if (limit["id"], execution_id, request_id) not in existing:
                create(conn, "resource_claim", resource_limit_id=limit["id"], execution_id=execution_id,
                       provider_request_id=request_id, units=1)


def configure_host_harness(conn, actor, host_id, harness_id, data: CatalogUpdate):
    require_admin(conn, actor)
    host = get(conn, "host", host_id, lock=True)
    table, link = tables["host_harness"], tables["host_harness_limit"]
    where = (table.c.host_id == host_id, table.c.harness_id == harness_id)
    old = conn.execute(select(table).where(*where)).mappings().first()
    if not old:
        raise DomainError("not_found", "Host harness is not registered", 404)
    allowed = set(CREATE_MODELS["host_harness"].model_fields) - {"host_id", "harness_id"}
    if set(data.changes) - allowed:
        raise DomainError("immutable_fields", "Host and harness identities cannot be changed", 422)
    previous_limits = set(conn.execute(select(link.c.resource_limit_id).where(
        link.c.host_id == host_id, link.c.harness_id == harness_id)).scalars())
    values = {**{key: old[key] for key in CREATE_MODELS["host_harness"].model_fields if key in old},
        "resource_limit_ids": list(previous_limits), **data.changes}
    parsed = CREATE_MODELS["host_harness"].model_validate(values).model_dump()
    identifiers = parsed.pop("resource_limit_ids")
    limits = [get(conn, "resource_limit", identifier, lock=True) for identifier in sorted(set(identifiers))]
    # The host revision covers its configuration, including this normalized join.
    row = change(conn, "host", host_id, data.expected_revision)
    conn.execute(update(table).where(*where).values(**parsed))
    conn.execute(delete(link).where(link.c.host_id == host_id, link.c.harness_id == harness_id))
    for identifier in set(identifiers):
        conn.execute(link.insert().values(host_id=host_id, harness_id=harness_id, resource_limit_id=identifier))
    attached = [limit for limit in limits if limit["id"] not in previous_limits]
    if attached:
        # Account for actual occupancy, even above the new ceiling. Detaching a
        # limit never proves an old process stopped or releases its reservation.
        _claim_attached_limits(conn, host_id, harness_id, attached)
    emit(conn, actor.id, None, "host", row, ["host_harness"], note=f"Configured harness {harness_id}")
    return {**parsed, "resource_limit_ids": identifiers, "host_revision": row["revision"]}


def validate_references(conn, kind, values, project_id, identifier=None):
    if kind == "reference":
        from .references import bibtex, reject_duplicate
        bibtex([values])
        reject_duplicate(conn, values, identifier)
    for column, value in values.items():
        if column.endswith("_id") and value and column != "project_id":
            field = tables[kind].c.get(column)
            if field is not None and field.foreign_keys:
                target = next(iter(field.foreign_keys)).column.table.name
                get(conn, target, value)
                other_project = project_of(conn, target, value)
                if project_id is not None and other_project is not None and other_project != project_id:
                    raise DomainError("scope_mismatch", "Referenced record belongs to another project", 422)
    for field, (join_name, own_key, other_key) in RELATIONS.get(kind, {}).items():
        target = next(iter(tables[join_name].c[other_key].foreign_keys)).column.table.name
        for linked_id in values[field]:
            same_project(conn, target, linked_id, project_id)
    if kind == "review_policy" and values["enabled"]:
        if values.get("maintainer_identity_id"):
            identity = get(conn, "integration_identity", values["maintainer_identity_id"])
            if any(get(conn, "repository", repo)["integration_id"] != identity["integration_id"] for repo in values["repository_ids"]):
                raise DomainError("identity_integration_mismatch", "Maintainer identity must belong to every policy repository's Forge", 422)
        policy, link = tables["review_policy"], tables["review_policy_repository"]
        query = select(policy.c.id).join(link).where(link.c.repository_id.in_(values["repository_ids"]),
            policy.c.enabled.is_(True), policy.c.phases.overlap(values["phases"]))
        if identifier:
            query = query.where(policy.c.id != identifier)
        if conn.execute(query).first():
            raise DomainError("ambiguous_review_policy", "Repository/phase is already covered by an enabled policy", 422)
    if kind == "node" and identifier:
        from ..missions.conditions import reject_cycle
        link = tables["node_dependency"]
        edges = {}
        for child, parent in conn.execute(select(link.c.child_node_id, link.c.parent_node_id)):
            edges.setdefault(str(child), set()).add(str(parent))
        edges[str(identifier)] = set(map(str, values["parent_node_ids"]))
        reject_cycle(edges)


def update_catalog(conn, actor, kind, identifier, data: CatalogUpdate):
    if kind == "workspace" and get(conn, kind, identifier)["status"] == "retired":
        raise DomainError("workspace_retired", "Retired workspaces are terminal; register a new independent checkout", 409)
    allowed = UPDATE_FIELDS.get(kind)
    if allowed is None or set(data.changes) - allowed:
        raise DomainError("immutable_fields", "Record changes contain immutable or unsupported fields", 422,
                          allowed=sorted(allowed or ()))
    old = get(conn, kind, identifier, lock=True)
    if kind == 'project' and 'workflow' in data.changes:
        if actor.kind != 'human':
            raise DomainError('human_required', 'Only a human may change the project workflow', 403)
        if old['workflow'] == 'milestones' and data.changes['workflow'] not in {'milestones', 'graph'}:
            raise DomainError('milestone_workflow_locked', 'Use the graph workflow for an explicit planning-policy transition', 409)
        run, mission = tables['run'], tables['mission']
        if conn.execute(select(run.c.id).join(mission, run.c.mission_id == mission.c.id).where(
                mission.c.project_id == identifier, run.c.status.in_(('active', 'paused', 'draining', 'stopping')))).first():
            raise DomainError('active_project_runs', 'Finish active runs before changing the project workflow', 409)
    project_id = project_of(conn, kind, identifier)
    if kind in OPERATOR_KINDS:
        require_admin(conn, actor)
    else:
        require_project(conn, actor, project_id, "maintainer" if kind in MAINTAINER_KINDS | {"project"} else "worker")
    values = {key: old[key] for key in CREATE_MODELS[kind].model_fields if key in old}
    for field, (join_name, own_key, other_key) in RELATIONS.get(kind, {}).items():
        link = tables[join_name]
        values[field] = list(conn.execute(select(link.c[other_key]).where(link.c[own_key] == identifier)).scalars())
    values.update(data.changes)
    values = CREATE_MODELS[kind].model_validate(values).model_dump()
    validate_references(conn, kind, values, project_id, identifier)
    updates = {key: json_value(values[key]) if isinstance(tables[kind].c[key].type, JSONB) else values[key]
               for key in data.changes if key not in RELATIONS.get(kind, {})}
    if kind == "reference":
        updates["status"] = values["status"]
    row = change(conn, kind, identifier, data.expected_revision, **updates)
    for field, (join_name, own_key, other_key) in RELATIONS.get(kind, {}).items():
        if field in data.changes:
            link = tables[join_name]
            conn.execute(delete(link).where(link.c[own_key] == identifier))
            for linked_id in set(values[field]):
                conn.execute(link.insert().values(**{own_key: identifier, other_key: linked_id}))
        row[field] = values[field]
    if kind in REVISIONED:
        snapshot(conn, kind, row, actor.id)
    changed_fields = list(data.changes)
    if kind == "reference" and old["status"] != row["status"] and "status" not in changed_fields:
        changed_fields.append("status")
    emit(conn, actor.id, project_id, kind if kind in REFERENCE_KINDS else None,
         row, changed_fields)
    return row


def create_catalog(conn, actor, kind, raw):
    if kind not in CREATE_MODELS or kind in {"run", "assignment", "automation", "obligation", "subscription", "project", "mission"}:
        raise DomainError("unknown_record", "This record is created through its lifecycle command", 422)
    data = CREATE_MODELS[kind].model_validate(raw)
    values = data.model_dump()
    project_id = values.get("project_id")
    if kind in OPERATOR_KINDS:
        require_admin(conn, actor)
    elif project_id:
        require_project(conn, actor, project_id, "maintainer" if kind in MAINTAINER_KINDS else "worker")
    else:
        raise DomainError("missing_scope", "Catalog record requires explicit project scope", 422)
    validate_references(conn, kind, values, project_id)
    if kind == 'roadmap_snapshot' and get(conn, 'project', project_id)['workflow'] == 'milestones':
        raise DomainError('milestone_approval_required', 'Use the verified milestone baseline approval operation', 409)
    if kind == "repository" and get(conn, "integration", data.integration_id)["kind"] != "forge":
        raise DomainError("invalid_integration", "Repository integration must be a Forge", 422)
    if kind == "document" or kind == "node":
        values["number"] = next_number(conn, kind, "project_id", project_id)
    if kind == "workspace":
        host = get(conn, "host", data.host_id)
        path, root = PurePosixPath(data.path), PurePosixPath(host["workspace_root"])
        if not path.is_relative_to(root) or path == root:
            raise DomainError("workspace_outside_root", "Workspace must be inside its configured host root", 422)
    relations = {}
    for field, (join_name, own_key, other_key) in RELATIONS.get(kind, {}).items():
        identifiers = values.pop(field)
        table = tables[join_name]
        target = next(iter(table.c[other_key].foreign_keys)).column.table.name
        for identifier in identifiers:
            same_project(conn, target, identifier, project_id)
        relations[field] = identifiers
    if kind == "host_harness":
        limits = values.pop("resource_limit_ids")
        conn.execute(tables[kind].insert().values(**values))
        for identifier in set(limits):
            conn.execute(tables["host_harness_limit"].insert().values(host_id=data.host_id,
                harness_id=data.harness_id, resource_limit_id=identifier))
        return values
    for column in tables[kind].c:
        if isinstance(column.type, JSONB) and column.name in values:
            values[column.name] = json_value(values[column.name])
    row = create(conn, kind, **values)
    for field, identifiers in relations.items():
        join_name, own_key, other_key = RELATIONS[kind][field]
        for identifier in set(identifiers):
            conn.execute(tables[join_name].insert().values(**{own_key: row["id"], other_key: identifier}))
    result = {**row, **relations}
    if kind in REVISIONED:
        snapshot(conn, kind, result, actor.id)
    if kind in REFERENCE_KINDS:
        emit(conn, actor.id, project_id, kind, row, list(values))
    return result
