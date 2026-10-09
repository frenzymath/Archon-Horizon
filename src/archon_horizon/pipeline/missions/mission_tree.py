"""Transactional invariants for the hierarchical mission tree.

Missions are the durable delegation context.  Assignment rows are attempts on
that context, so queue mutations must not silently widen a child mission or
leave a closed parent with runnable descendants.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select

from ..auth import Actor, live_execution
from ..errors import DomainError
from ..persistence.records import change, emit, get, same_project, snapshot
from ..persistence.schema import tables


def normalized_scope(value: dict | None) -> dict:
    value = value or {}
    return {
        "node_ids": sorted({str(item) for item in value.get("node_ids", [])}),
        "document_ids": sorted({str(item) for item in value.get("document_ids", [])}),
        "repository_paths": sorted(
            ({"repository_id": str(item["repository_id"]), "path": item.get("path")} for item in value.get("repository_paths", [])),
            key=lambda item: (item["repository_id"], item["path"] or ""),
        ),
    }


def scope_contains(parent: dict | None, child: dict | None) -> bool:
    """Return whether child is no broader than parent.

    An empty scope means project-wide only for a root mission.  A delegated
    child must carry an explicit scope, or inherit its parent's scope before
    this check is called.
    """
    parent = normalized_scope(parent)
    child = normalized_scope(child)
    if not any(parent.values()):
        return True
    if not any(child.values()):
        return False
    if not set(child["node_ids"]).issubset(parent["node_ids"]):
        return False
    if not set(child["document_ids"]).issubset(parent["document_ids"]):
        return False
    allowed = {(row["repository_id"], row["path"]) for row in parent["repository_paths"]}
    for row in child["repository_paths"]:
        key = (row["repository_id"], row["path"])
        if key in allowed:
            continue
        # A repository-wide parent contains every path in that repository.
        if (row["repository_id"], None) in allowed:
            continue
        if row["path"] is not None and any(
                repository == row["repository_id"] and path is not None
                and row["path"].startswith(path + "/") for repository, path in allowed):
            continue
        return False
    return True


def validate_scope_references(conn, project_id: UUID, scope: dict) -> None:
    for kind in ("node", "document"):
        for identifier in scope[f"{kind}_ids"]:
            same_project(conn, kind, identifier, project_id)
    for item in scope["repository_paths"]:
        same_project(conn, "repository", item["repository_id"], project_id)


def validate_scope_links(scope: dict, node_ids: list[UUID], document_ids: list[UUID]) -> None:
    """Explicit scopes must include the mission's linked graph records."""
    if not any(scope.values()):
        return
    if not set(map(str, node_ids)).issubset(scope["node_ids"]):
        raise DomainError("scope_link_mismatch", "Mission node links must remain inside its explicit scope", 422)
    if not set(map(str, document_ids)).issubset(scope["document_ids"]):
        raise DomainError("scope_link_mismatch", "Mission document links must remain inside its explicit scope", 422)


def contains_mission(conn, ancestor_id: UUID, child_id: UUID) -> bool:
    table = tables["mission"]
    lineage = select(table.c.id, table.c.parent_id).where(table.c.id == child_id).cte("mission_lineage", recursive=True)
    lineage = lineage.union(select(table.c.id, table.c.parent_id).join(lineage, table.c.id == lineage.c.parent_id))
    return bool(conn.execute(select(lineage.c.id).where(lineage.c.id == ancestor_id)).first())


def _open_children(conn, mission_id: UUID, *, exclude: UUID | None = None) -> int:
    mission = tables["mission"]
    query = select(func.count()).select_from(mission).where(
        mission.c.parent_id == mission_id, mission.c.status == "open")
    if exclude:
        query = query.where(mission.c.id != exclude)
    return int(conn.execute(query).scalar_one())


def parent_for_child(conn, project_id: UUID, parent_id: UUID | None,
                     expected_parent_revision: int | None) -> dict | None:
    if parent_id is None:
        if expected_parent_revision is not None:
            raise DomainError("invalid_parent_revision", "A parent revision requires a parent mission", 422)
        return None
    parent = same_project(conn, "mission", parent_id, project_id)
    if parent["status"] != "open":
        raise DomainError("parent_closed", "A child can only be delegated from an open mission", 409)
    if expected_parent_revision is None:
        raise DomainError("parent_revision_required", "Child delegation requires the current parent revision", 422)
    if parent["revision"] != expected_parent_revision:
        raise DomainError("revision_conflict", "The parent mission changed; refresh before delegating",
                          current_revision=parent["revision"])
    open_count = _open_children(conn, parent_id)
    if open_count >= parent["max_open_children"]:
        mission = tables["mission"]
        children = conn.execute(select(mission.c.id, mission.c.number, mission.c.title,
            mission.c.status, mission.c.revision).where(mission.c.parent_id == parent_id,
                mission.c.status == "open").order_by(mission.c.number).limit(20)).mappings()
        raise DomainError("child_budget_exhausted",
            "The mission child budget is exhausted. Reconcile delivered child missions, "
            "or explicitly adjust max_open_children through MissionUpdate for distinct remaining work. "
            "This contract is separate from worker capacity.", 409,
            parent_id=str(parent_id), parent_revision=parent["revision"],
            parent_detail_url=f"/api/v3/records/mission/{parent_id}",
            max_open_children=parent["max_open_children"], open_count=open_count,
            open_children=[{**dict(row), "id": str(row["id"]),
                "detail_url": f"/api/v3/records/mission/{row['id']}"} for row in children],
            open_children_truncated=open_count > 20,
            update_contract="MissionUpdate", update_url=f"/api/v3/missions/{parent_id}",
            schema_url="/api/v3/schema?section=mission_update")
    return parent


def validate_child(parent: dict, scope: dict | None,
                   acceptance_criteria: list[str], delegation_note: str | None) -> dict:
    inherited = parent["scope"] or {}
    effective = normalized_scope(scope if scope is not None else inherited)
    if not scope_contains(inherited, effective):
        raise DomainError("scope_widening", "A child mission must remain within its parent's scope", 422)
    if not acceptance_criteria:
        raise DomainError("acceptance_criteria_required", "Delegated missions require acceptance criteria", 422)
    if not delegation_note or not delegation_note.strip():
        raise DomainError("delegation_note_required", "Delegation requires a concrete handoff note", 422)
    return effective


def validate_reparent(conn, mission: dict, parent_id: UUID | None) -> None:
    if parent_id is None:
        return
    if contains_mission(conn, mission["id"], parent_id):
        raise DomainError("mission_cycle", "A mission cannot be placed under itself or one of its descendants", 422)


def require_mission_authority(conn, actor: Actor, mission_id: UUID, *, role: str = "worker") -> dict:
    mission = get(conn, "mission", mission_id)
    if actor.kind != "agent":
        return mission
    execution = live_execution(conn, actor)
    if execution["project_id"] != mission["project_id"]:
        raise DomainError("forbidden", "The mission is outside this execution's project", 403)
    if execution["role"] not in ("maintainer", role):
        raise DomainError("forbidden", "The execution role cannot mutate this mission", 403)
    assignment = get(conn, "assignment", execution["assignment_id"])
    owned = assignment["mission_id"]
    run = get(conn, "run", assignment["run_id"])
    # Only a server-created objective planner has objective-wide planning
    # authority. Editing functions=['planner'] cannot grant this capability.
    if assignment["automation_id"] and run.get("orchestration") == "objective":
        automation = get(conn, "automation", assignment["automation_id"])
        if automation["name"] == "objective-planner":
            owned = run["mission_id"]
    if assignment["role"] == "maintainer" and run.get("orchestration") == "objective":
        # Item-specific maintainers accept and arrange repair of objective work,
        # including work originally dispatched by a different planning pass.
        owned = run["mission_id"]
    if not contains_mission(conn, owned, mission_id):
        raise DomainError("forbidden", "The mission is outside this execution's delegated subtree", 403)
    return mission


def require_assignment_authority(conn, actor: Actor, assignment: dict) -> None:
    if actor.kind != "agent":
        return
    execution = live_execution(conn, actor)
    if assignment["run_id"] != execution["run_id"]:
        raise DomainError("forbidden", "The assignment belongs to another run", 403)
    require_mission_authority(conn, actor, assignment["mission_id"])


def ensure_closure_allowed(conn, mission: dict, *, cancel: bool = False) -> None:
    table = tables["mission"]
    descendants = select(table.c.id).where(table.c.parent_id == mission["id"]).cte("mission_descendants", recursive=True)
    descendants = descendants.union(select(table.c.id).join(descendants, table.c.parent_id == descendants.c.id))
    substantive = conn.execute(select(func.count()).select_from(table).where(
        table.c.id.in_(select(descendants.c.id)), table.c.status == "open")).scalar_one()
    if substantive:
        blocking = conn.execute(select(table.c.id, table.c.number, table.c.title, table.c.status,
            table.c.revision, table.c.parent_id).where(table.c.id.in_(select(descendants.c.id)),
                table.c.status == "open").order_by(table.c.number).limit(20)).mappings()
        raise DomainError("open_children",
            "Cannot close while an open child mission remains. Settle the listed missions; "
            "finishing or cancelling assignments does not close their missions.", 409,
            blocking_mission_count=substantive,
            blocking_missions=[{**dict(row), "id": str(row["id"]), "parent_id": str(row["parent_id"]),
                "detail_url": f"/api/v3/records/mission/{row['id']}"} for row in blocking],
            blocking_missions_truncated=substantive > 20)
    if not cancel:
        return
    assignment = tables["assignment"]
    live = conn.execute(select(func.count()).select_from(assignment).where(
        (assignment.c.mission_id == mission["id"]) | assignment.c.mission_id.in_(select(descendants.c.id)),
        assignment.c.status.in_(("pending", "running", "stopping")))).scalar_one()
    if live:
        raise DomainError("active_assignments", "Settle assignments before closing this mission", 409)


def bump_parent(conn, parent: dict, actor_id: UUID) -> dict:
    row = change(conn, "mission", parent["id"], parent["revision"], delegation_note=parent["delegation_note"])
    snapshot(conn, "mission", row, actor_id)
    emit(conn, actor_id, row["project_id"], "mission", row, ["child_ids"])
    return row
