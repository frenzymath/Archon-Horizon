"""Transactional project, mission, queue and context commands.

Callers own a short database transaction and acquire records.transaction_lock
before invoking mutations. Provider/network work is always outside that scope.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import BigInteger, String, cast, func, select, update

from .. import models
from ..auth import Actor, live_execution, require_admin, require_project
from .conditions import Evaluation, Truth, evaluate, referenced_objects, reject_cycle
from ..errors import DomainError
from ..persistence.records import change, create, emit, get, json_value, next_number, project_of, same_project, snapshot
from ..persistence.schema import tables
from .mission_tree import (
    bump_parent, contains_mission, normalized_scope, parent_for_child, require_mission_authority,
    scope_contains, validate_child, validate_reparent, validate_scope_links, validate_scope_references,
)

FUNCTIONS = {"planner", "debugger", "reviewer", "orchestrator"}
LIVE_EXECUTIONS = ("starting", "running", "stopping")
TERMINAL_ASSIGNMENTS = ("completed", "failed", "cancelled")
EXTERNAL_DELIVERIES = ("zulip_post", "forge_label", "forge_review", "forge_merge", "forge_create",
                       "forge_comment", "forge_change", "forge_edit")


class Service:
    def __init__(self, store, config):
        self.store = store
        self.config = config

    def project(self, conn, actor: Actor, data: models.ProjectCreate) -> dict:
        require_admin(conn, actor)
        row = create(conn, "project", **data.model_dump())
        snapshot(conn, "project", row, actor.id)
        emit(conn, actor.id, row["id"], "project", row, list(data.model_fields_set))
        return row

    def mission(self, conn, actor: Actor, data: models.MissionCreate) -> dict:
        require_project(conn, actor, data.project_id, "worker")
        if actor.kind == "agent" and data.parent_id is None:
            raise DomainError("forbidden", "Agent sessions may only create missions under their owned mission", 403)
        parent = parent_for_child(conn, data.project_id, data.parent_id, data.expected_parent_revision)
        scope = data.scope.model_dump(mode="json") if data.scope else None
        effective_scope = validate_child(parent, scope, data.acceptance_criteria, data.delegation_note) if parent else normalized_scope(scope)
        validate_scope_references(conn, data.project_id, effective_scope)
        validate_scope_links(effective_scope, data.node_ids, data.document_ids)
        if parent:
            require_mission_authority(conn, actor, parent["id"])
        if data.roadmap_document_id:
            roadmap = same_project(conn, "document", data.roadmap_document_id, data.project_id)
            if roadmap["kind"] != "roadmap":
                raise DomainError("invalid_roadmap", "Mission roadmap must reference a roadmap document", 422)
        for kind, identifiers in (("node", data.node_ids), ("document", data.document_ids)):
            for identifier in identifiers:
                same_project(conn, kind, identifier, data.project_id)
        values = data.model_dump(exclude={"node_ids", "document_ids", "expected_parent_revision"})
        values["scope"] = effective_scope
        values.pop("parent_id", None)
        values["parent_id"] = data.parent_id
        values["number"] = next_number(conn, "mission", "project_id", data.project_id)
        row = create(conn, "mission", **values)
        for kind, identifiers in (("node", data.node_ids), ("document", data.document_ids)):
            for identifier in set(identifiers):
                conn.execute(tables[f"mission_{kind}"].insert().values(
                    mission_id=row["id"], **{f"{kind}_id": identifier}))
        snapshot(conn, "mission", row, actor.id)
        emit(conn, actor.id, data.project_id, "mission", row, list(values))
        if parent:
            bump_parent(conn, parent, actor.id)
        return row

    def update_mission(self, conn, actor: Actor, identifier: UUID, data: models.MissionUpdate) -> dict:
        """Revise an open owned contract without dropping linked or delegated scope.

        Parent movement uses the new parent's revision and budget. A real change
        snapshots the contract and queues revision-pinned goals; no-op patches
        retain the current revision after the same authority checks.
        """
        old = get(conn, "mission", identifier, lock=True)
        require_project(conn, actor, old["project_id"], "worker")
        require_mission_authority(conn, actor, identifier)
        if old["revision"] != data.expected_revision:
            raise DomainError("revision_conflict", "The record changed; refresh and retry", current_revision=old["revision"])
        if old["status"] != "open":
            raise DomainError("mission_closed", "Reopen a mission explicitly before editing its contract", 409)
        values = data.model_dump(exclude_unset=True, exclude={"expected_revision", "expected_parent_revision"})
        if actor.kind == "agent" and "acceptance_criteria" in values:
            current = get(conn, "assignment", live_execution(conn, actor)["assignment_id"])
            if (get(conn, "run", current["run_id"]).get("orchestration") == "objective" and current["role"] != "maintainer"
                    and not set(old["acceptance_criteria"]) <= set(values["acceptance_criteria"])):
                raise DomainError("acceptance_decision_required", "Workers may add criteria; removing or replacing required outcomes needs a maintainer decision", 403)
        parent_changed = "parent_id" in values and values["parent_id"] != old["parent_id"]
        old_parent = get(conn, "mission", old["parent_id"]) if old["parent_id"] else None
        parent = old_parent
        if parent_changed:
            if actor.kind == "agent":
                execution = live_execution(conn, actor)
                owned_mission = get(conn, "assignment", execution["assignment_id"])["mission_id"]
                if owned_mission == identifier:
                    raise DomainError("forbidden", "A session cannot move its own authority root", 403)
            validate_reparent(conn, old, values["parent_id"])
            parent = parent_for_child(conn, old["project_id"], values["parent_id"], data.expected_parent_revision)
            if actor.kind == "agent" and parent is None:
                raise DomainError("forbidden", "An agent cannot detach a mission from its delegated tree", 403)
            if parent:
                require_mission_authority(conn, actor, parent["id"])
        elif data.expected_parent_revision is not None:
            raise DomainError("invalid_parent_revision", "The parent revision is only supplied when moving a mission", 422)
        effective_scope = normalized_scope(data.scope.model_dump(mode="json") if "scope" in values else old["scope"])
        validate_scope_references(conn, old["project_id"], effective_scope)
        if parent:
            effective_scope = validate_child(parent, effective_scope,
                values.get("acceptance_criteria", old["acceptance_criteria"]),
                values.get("delegation_note", old["delegation_note"]))
        if "scope" in values:
            values["scope"] = effective_scope
            linked = {}
            for kind in ("node", "document"):
                relation = tables[f"mission_{kind}"]
                linked[kind] = list(conn.execute(select(relation.c[f"{kind}_id"]).where(
                    relation.c.mission_id == identifier)).scalars())
            validate_scope_links(effective_scope, linked["node"], linked["document"])
            children = conn.execute(select(tables["mission"].c.scope).where(tables["mission"].c.parent_id == identifier)).scalars()
            if any(not scope_contains(effective_scope, child) for child in children):
                raise DomainError("child_scope_conflict", "The narrowed scope excludes an existing child mission", 409)
        if "max_open_children" in values:
            count = conn.execute(select(func.count()).select_from(tables["mission"]).where(
                tables["mission"].c.parent_id == identifier, tables["mission"].c.status == "open")).scalar_one()
            if count > values["max_open_children"]:
                raise DomainError("child_budget_conflict", "The child budget is below the existing open child count", 409)
        if values.get("roadmap_document_id"):
            doc = same_project(conn, "document", values["roadmap_document_id"], old["project_id"])
            if doc["kind"] != "roadmap":
                raise DomainError("invalid_roadmap", "Expected a roadmap document", 422)
        # Repeating an already applied patch is not a new goal. Revision and
        # authority checks above still apply, including to an empty patch.
        values = {name: value for name, value in values.items() if value != old.get(name)}
        if not values:
            return old
        row = change(conn, "mission", identifier, data.expected_revision, **values)
        revision_id = snapshot(conn, "mission", row, actor.id)
        self.goal_updates(conn, actor, identifier, revision_id)
        emit(conn, actor.id, row["project_id"], "mission", row, list(values))
        if parent_changed:
            if old_parent:
                bump_parent(conn, old_parent, actor.id)
            if parent:
                bump_parent(conn, parent, actor.id)
        return row

    def goal_updates(self, conn, actor: Actor, mission_id: UUID, revision_id: UUID) -> None:
        thread, assignment, run = (tables[name] for name in ("provider_thread", "assignment", "run"))
        rows = conn.execute(select(thread.c.id, run.c.id.label("run_id"), run.c.revision,
                                  run.c.adopted_roadmap_snapshot_id).join(
            assignment, thread.c.assignment_id == assignment.c.id).join(run, assignment.c.run_id == run.c.id
        ).where(assignment.c.mission_id == mission_id, thread.c.kind == "primary",
                thread.c.status.in_(("creating", "available")))).mappings()
        for row in rows:
            payload = {"provider_thread_id": str(row["id"]), "mission_revision_id": str(revision_id),
                       "run_revision": row["revision"],
                       "roadmap_snapshot_id": str(row["adopted_roadmap_snapshot_id"]) if row["adopted_roadmap_snapshot_id"] else None}
            outbox = tables["outbox_operation"]
            # Superseded unsent updates are settled; an in-flight update remains reconcilable.
            conn.execute(update(outbox).where(outbox.c.kind == "goal_update", outbox.c.status == "pending",
                outbox.c.payload["provider_thread_id"].astext == str(row["id"])
            ).values(status="completed", revision=outbox.c.revision + 1, updated_at=func.now()))
            from sqlalchemy.dialects.postgresql import insert
            conn.execute(insert(outbox).values(project_id=project_of(conn, "run", row["run_id"]),
                actor_principal_id=actor.id, kind="goal_update", schema_version=1,
                idempotency_key=f"goal:{row['id']}:{revision_id}:{row['revision']}", payload=payload).on_conflict_do_nothing())

    def reconcile_goal_updates(self, conn, *, thread_id: UUID | None = None) -> int:
        """Settle unsent updates already consumed or targeting terminal contexts.

        Applied revisions are worker completion evidence. A newer revision of
        the same mission subsumes an older goal, provided its run and roadmap
        inputs are also accounted for. Leave in-flight/uncertain delivery alone.
        """
        outbox, thread, assignment = (tables[name] for name in
                                      ("outbox_operation", "provider_thread", "assignment"))
        desired = tables["record_revision"].alias("desired_mission")
        applied = tables["record_revision"].alias("applied_mission")
        reference = tables["object_reference"]
        consumed = select(outbox.c.id).select_from(outbox.join(thread,
            outbox.c.payload["provider_thread_id"].astext == cast(thread.c.id, String)
        ).join(assignment, thread.c.assignment_id == assignment.c.id).join(desired,
            outbox.c.payload["mission_revision_id"].astext == cast(desired.c.id, String)
        ).join(applied, thread.c.applied_mission_revision_id == applied.c.id).join(reference,
            desired.c.object_id == reference.c.id
        )).where(
            outbox.c.kind == "goal_update", outbox.c.status == "pending",
            desired.c.object_id == applied.c.object_id,
            reference.c.mission_id == assignment.c.mission_id,
            desired.c.object_revision <= applied.c.object_revision,
            cast(outbox.c.payload["run_revision"].astext, BigInteger) <= thread.c.applied_run_revision,
            outbox.c.payload["roadmap_snapshot_id"].astext.is_not_distinct_from(
                cast(thread.c.applied_roadmap_snapshot_id, String)),
        )
        if thread_id is not None:
            consumed = consumed.where(thread.c.id == thread_id)
        settled = conn.execute(update(outbox).where(
            outbox.c.kind == "goal_update", outbox.c.status == "pending", outbox.c.id.in_(consumed),
        ).values(status="completed", retry_at=None, lease_owner=None, lease_expires_at=None,
                 failure=None, revision=outbox.c.revision + 1, updated_at=func.now()).returning(outbox.c.id))
        count = len(settled.fetchall())
        terminal_threads = select(cast(thread.c.id, String)).select_from(
            thread.join(assignment, thread.c.assignment_id == assignment.c.id)
        ).where(assignment.c.status.in_(TERMINAL_ASSIGNMENTS))
        if thread_id is not None:
            terminal_threads = terminal_threads.where(thread.c.id == thread_id)
        result = conn.execute(update(outbox).where(
            outbox.c.kind == "goal_update", outbox.c.status == "pending",
            outbox.c.payload["provider_thread_id"].astext.in_(terminal_threads),
        ).values(status="cancelled", retry_at=None, lease_owner=None, lease_expires_at=None,
                 failure={"kind": "lifecycle", "code": "target_terminal",
                          "message": "Goal update superseded because its assignment is terminal"},
                 revision=outbox.c.revision + 1, updated_at=func.now())
        .returning(outbox.c.id))
        return count + len(result.fetchall())

    def assignment(self, conn, actor: Actor, data: models.AssignmentCreate,
                   *, automation_id: UUID | None = None, internal: bool = False, repair: bool = False) -> dict:
        run = get(conn, "run", data.run_id, lock=True)
        project_id = project_of(conn, "run", data.run_id)
        require_project(conn, actor, project_id, "worker")
        if data.role == "maintainer" and not internal:
            require_project(conn, actor, project_id, "maintainer")
        if repair:
            require_project(conn, actor, project_id, "maintainer")
        if run["status"] != "active" and not (repair and run["status"] == "draining"):
            raise DomainError("run_not_active", "New assignments require an active run")
        if run.get("orchestration") == "objective" and "orchestrator" in data.functions:
            raise DomainError("objective_profile_managed", "Objective runs use one bounded planner and item-specific maintenance", 422)
        if "orchestrator" in data.functions and not run["phase"].get("orchestrated"):
            raise DomainError("retired_orchestrator", "The orchestrator profile is available only to already-persisted legacy runs", 422)
        if run.get("pending_phase"):
            raise DomainError("phase_settling", "The accepted phase is settling; new work waits for the next phase", 409)
        same_project(conn, "mission", data.mission_id, project_id)
        mission = get(conn, "mission", data.mission_id)
        if actor.kind == "agent":
            current = get(conn, "assignment", live_execution(conn, actor)["assignment_id"])
            if current["mission_id"] == data.mission_id:
                raise DomainError("child_mission_required", "Continue your session; a queued session needs a different, bounded mission", 422)
            if (run.get("orchestration") == "objective" and
                    " ".join(mission["objective"].split()) == " ".join(get(conn, "mission", current["mission_id"])["objective"].split())):
                raise DomainError("unchanged_mission", "A renamed mission cannot replace your unchanged goal; narrow the delegated outcome", 422)
        if mission["status"] != "open" and not (repair and mission["status"] == "completed" and run["status"] == "draining"):
            raise DomainError("mission_closed", "Assignments require an open mission", 409)
        if data.parent_id and get(conn, "assignment", data.parent_id)["run_id"] != data.run_id:
            raise DomainError("scope_mismatch", "Delegator must belong to the same run", 422)
        if actor.kind == "agent" and data.parent_id is not None:
            if live_execution(conn, actor)["assignment_id"] != data.parent_id:
                raise DomainError("forbidden", "Agents may only delegate from their own assignment", 403)
            require_mission_authority(conn, actor, data.mission_id)
        if actor.kind == "agent" and not internal and data.parent_id is None:
            raise DomainError("delegator_required", "Agent delegation requires the current parent assignment", 422)
        if data.parent_id:
            delegator = get(conn, "assignment", data.parent_id)
            # An assignment may only delegate the mission it owns or one of its descendants.
            delegation_root = delegator["mission_id"]
            if delegator["automation_id"] and run.get("orchestration") == "objective":
                if get(conn, "automation", delegator["automation_id"])["name"] == "objective-planner":
                    delegation_root = run["mission_id"]
            if not contains_mission(conn, delegation_root, mission["id"]):
                raise DomainError("scope_mismatch", "Delegated mission must be within the parent assignment mission", 422)
            if actor.kind == "agent" and mission["id"] == delegator["mission_id"]:
                raise DomainError("child_mission_required", "Create a narrower child mission before delegating an assignment", 422)
        if not contains_mission(conn, run["mission_id"], mission["id"]):
            raise DomainError("scope_mismatch", "Assignments must belong to the run's mission tree", 422)
        if mission["parent_id"] is not None:
            owner = conn.execute(select(tables["assignment"].c.id).where(
                tables["assignment"].c.run_id == data.run_id, tables["assignment"].c.mission_id == data.mission_id,
                tables["assignment"].c.role == data.role, tables["assignment"].c.status.in_(("pending", "running", "stopping"))).limit(1)).first()
            if owner:
                raise DomainError("mission_already_owned", "This mission already has an active assignment for that role", 409,
                                  assignment_id=str(owner[0]))
        unknown = set(data.functions) - FUNCTIONS
        if unknown:
            raise DomainError("unknown_function", "Functions must exist in the pinned catalog", 422,
                              functions=sorted(unknown))
        if data.reviewer_descriptor_id:
            if not internal:
                raise DomainError("review_manifest_required", "Prepare reviewer assignments through /api/v3/reviewer-assignments", 422)
            same_project(conn, "reviewer_descriptor", data.reviewer_descriptor_id, project_id)
        condition = data.start_condition.model_dump(mode="json") if data.start_condition else None
        self.validate_condition(conn, project_id, condition)
        table = tables["assignment"]
        from ..execution.queue_policies import validate_enqueue
        category, options, harness_id = validate_enqueue(conn, run, data)
        rank = conn.execute(select(func.coalesce(func.max(table.c.queue_rank), 0) + 1024).where(
            table.c.run_id == data.run_id)).scalar_one()
        values = data.model_dump(exclude={"start_condition", "model_options"})
        values.update(category=category, harness_id=harness_id)
        row = create(conn, "assignment", **values, start_condition=condition,
                     model_options=options,
                     automation_id=automation_id, number=next_number(conn, "assignment", "run_id", data.run_id),
                     queue_rank=rank)
        snapshot(conn, "assignment", row, actor.id)
        # Planning and supervision account for bounded decisions; ordinary
        # workers and maintainers retain their mathematical mission deliverable.
        mission = get(conn, "mission", data.mission_id)
        kind, description = "deliverable", mission["objective"]
        if "orchestrator" in data.functions:
            kind, description = "decision", "Account for one bounded Horizon health and queue-consistency episode."
        elif "planner" in data.functions:
            kind, description = "decision", (
                "Account for one bounded planning pass: identify concrete unowned work "
                "or preserve existing owners and their wake conditions.")
        elif automation_id:
            from ..execution.root_maintenance import NAME
            if get(conn, "automation", automation_id)["name"] == NAME:
                kind, description = "decision", (
                    "Account for this bounded maintenance pass with evidence: plan or repair concrete work, "
                    "review delivered results, preserve an existing owner and its event wait, or record phase completion.")
        create(conn, "obligation", assignment_id=row["id"], number=1,
               description=description, kind=kind)
        from ..integrations.communications import seed_assignment_subscriptions
        seed_assignment_subscriptions(conn, row)
        emit(conn, actor.id, project_id, "assignment", row, list(values))
        return row

    def validate_condition(self, conn, project_id: UUID, condition: dict | None,
                           assignment_id: UUID | None = None) -> None:
        if condition is None:
            return
        models.Condition.model_validate(condition)
        for ref in referenced_objects(condition):
            kind = ref["kind"]
            if kind in ("file", "directory"):
                same_project(conn, "repository", ref["repository_id"], project_id)
            else:
                same_project(conn, kind, ref["id"], project_id)
        if assignment_id:
            assignment, run, mission = tables["assignment"], tables["run"], tables["mission"]
            rows = conn.execute(select(assignment.c.id, assignment.c.start_condition).join(run,
                assignment.c.run_id == run.c.id).join(mission, run.c.mission_id == mission.c.id).where(
                assignment.c.status.in_(("pending", "running")), mission.c.project_id == project_id)).mappings()
            edges = {}
            conditions = {row["id"]: row["start_condition"] for row in rows}
            conditions[assignment_id] = condition
            for owner_id, current in conditions.items():
                dependencies = set()
                for ref in referenced_objects(current):
                    if ref["kind"] == "assignment":
                        dependencies.add(str(ref["id"]))
                    elif ref["kind"] == "obligation":
                        obligation = get(conn, "obligation", ref["id"])
                        if obligation["status"] == "open":
                            dependencies.add(str(obligation["assignment_id"]))
                edges[str(owner_id)] = dependencies
            reject_cycle(edges)

    def readiness(self, conn, row: dict, now: datetime, *, visited: frozenset = frozenset(), observations: dict | None = None) -> Evaluation:
        observations = {} if observations is None else observations
        if row["status"] != "pending":
            return Evaluation(Truth.FALSE, row["status"].capitalize())
        if row.get("pause_reason"):
            return Evaluation(Truth.FALSE, "Session paused: " + row["pause_reason"])
        if row["expires_at"] and row["expires_at"] <= now:
            return Evaluation(Truth.FALSE, "Start window expired")
        if row.get("checkpoint_requested_at") and self.pending_deliveries(conn, row["id"]):
            notices = tables["notification"]
            wake = conn.execute(select(notices.c.id).where(notices.c.assignment_id == row["id"],
                notices.c.urgency == "control", notices.c.disposition == "pending",
                notices.c.created_at >= row["checkpoint_requested_at"]).limit(1)).first()
            outbox = tables["outbox_operation"]
            wake = wake or conn.execute(self.assignment_deliveries(row["id"]).with_only_columns(outbox.c.id).where(
                outbox.c.status.in_(("completed", "failed", "cancelled")),
                outbox.c.updated_at > row["checkpoint_requested_at"]).limit(1)).first()
            if not wake:
                return Evaluation(Truth.FALSE, "Waiting for durable Forge or Zulip delivery; context is preserved")
        run_key = ("run", row["run_id"])
        if run_key not in observations:
            observations[run_key] = get(conn, "run", row["run_id"])
        run = observations[run_key]
        if run["status"] not in ("active", "draining") or (run["status"] == "draining" and row["automation_id"]):
            return Evaluation(Truth.FALSE, f"Run is {run['status']}")
        if row["automation_id"]:
            key = ("automation", row["automation_id"])
            if key not in observations:
                observations[key] = get(conn, "automation", row["automation_id"])
            if not observations[key]["enabled"]:
                return Evaluation(Truth.FALSE, "Recurring automation is disabled")
            from ..execution.root_maintenance import admission_blocker
            root_blocker = admission_blocker(conn, row, observations[key])
            if root_blocker:
                return Evaluation(Truth.FALSE, root_blocker)
            from ..review.backlog import recurring_maintenance_blocker
            ownership_blocker = recurring_maintenance_blocker(conn, row, observations[key])
            if ownership_blocker:
                return Evaluation(Truth.FALSE, ownership_blocker)
        from ..execution.admission import reason, run_usage
        usage_key = ("run_usage", run["id"])
        if usage_key not in observations:
            observations[usage_key] = run_usage(conn, run)
        limit = reason(run, observations[usage_key], now, already_started=row["started_at"] is not None,
                       supervisory="orchestrator" in (row.get("functions") or []))
        if limit:
            return Evaluation(Truth.FALSE, limit)
        for key, label in (("not_before", "Deferred"), ("retry_at", "Recovery backoff")):
            if row[key] and row[key] > now:
                return Evaluation(Truth.FALSE, f"{label} until {row[key].isoformat()}", row[key])
        if row["id"] in visited:
            return Evaluation(Truth.UNKNOWN, "Recursive readiness dependency")
        visited = visited | {row["id"]}
        return self.condition_readiness(conn, row, now, visited=visited, observations=observations)

    def condition_readiness(self, conn, row: dict, now: datetime, *, visited: frozenset = frozenset(),
                            observations: dict | None = None, events_only: bool = False) -> Evaluation:
        observations = {} if observations is None else observations

        def observe_uncached(expr):
            op = expr["op"]
            if op == "planning_needed":
                from ..execution.coordination import planning_needed
                return planning_needed(conn, self, row, expr["run_id"], now, visited, observations)
            if op == "revision_after":
                target = expr["target"]
                revision = get(conn, target["kind"], target["id"]).get("revision")
                if revision is None:
                    return Evaluation(Truth.UNKNOWN, "Target does not expose revisions")
                return Evaluation(Truth.TRUE if revision > expr["revision"] else Truth.FALSE,
                                  f"{target['kind']} revision {revision}; waiting after {expr['revision']}")
            if op == "discussion_changed":
                from ..integrations.communications import discussion_fingerprint
                discussion = get(conn, "discussion", expr["discussion_id"])
                if discussion["sync_status"] != "current":
                    return Evaluation(Truth.UNKNOWN, "Discussion observation is not current")
                changed = discussion_fingerprint(conn, discussion["id"]) != expr["fingerprint"]
                return Evaluation(Truth.TRUE if changed else Truth.FALSE,
                                  "Discussion changed" if changed else "Waiting for a new or edited discussion message")
            if op == "status_in":
                target = expr["target"]
                value = get(conn, target["kind"], target["id"])["status"]
                return Evaluation(Truth.TRUE if value in expr["values"] else Truth.FALSE,
                                  f"{target['kind']} is {value}")
            if op == "obligation_accounted":
                value = get(conn, "obligation", expr["obligation_id"])["status"]
                return Evaluation(Truth.FALSE if value == "open" else Truth.TRUE, f"Obligation is {value}")
            if op == "publication_verified":
                value = get(conn, "publication", expr["publication_id"])["status"]
                return Evaluation(Truth.TRUE if value == "verified" else Truth.FALSE, f"Publication is {value}")
            if op == "queue_below":
                table = tables["assignment"]
                candidates = conn.execute(select(table).where(table.c.run_id == UUID(str(expr["run_id"])),
                    table.c.status == "pending", table.c.automation_id.is_(None), table.c.id != row["id"])).mappings()
                count = sum(self.readiness(conn, dict(candidate), now, visited=visited, observations=observations).ready for candidate in candidates)
                return Evaluation(Truth.TRUE if count < expr["count"] else Truth.FALSE,
                                  f"{count} ready assignments; threshold {expr['count']}")
            if op in ("forge_open_count", "forge_actionable_count"):
                repository, cursor = tables["repository"], tables["connector_cursor"]
                repos = list(conn.execute(select(repository).where(
                    repository.c.project_id == UUID(str(expr["project_id"])),
                    repository.c.archived_at.is_(None))).mappings())
                if expr["repository_ids"]:
                    repos = [repo for repo in repos if str(repo["id"]) in {str(value) for value in expr["repository_ids"]}]
                if not repos:
                    return Evaluation(Truth.UNKNOWN, "No matching repositories have been observed")
                for repo in repos:
                    health = conn.execute(select(cursor).where(cursor.c.integration_id == repo["integration_id"],
                        cursor.c.consumer == "forge:" + str(repo["id"]))).mappings().first()
                    if (not health or health["status"] != "current" or not health["last_synced_at"]
                            or (now - health["last_synced_at"]).total_seconds() > 3 * self.config.connector_interval_seconds):
                        return Evaluation(Truth.UNKNOWN, "Forge observation is unavailable or stale")
                item = tables["forge_item"]
                query = select(func.count()).select_from(item).where(item.c.repository_id.in_([repo["id"] for repo in repos]),
                    item.c.status == "open", item.c.kind.in_(expr["kinds"]))
                if expr.get("origin_run_id"):
                    query = query.where(item.c.origin_run_id == UUID(str(expr["origin_run_id"])))
                if expr.get("review_phase"):
                    query = query.where(item.c.review_phase == expr["review_phase"])
                if op == "forge_actionable_count":
                    from ..review.backlog import current_objection
                    query = query.where(~current_objection(item))
                if expr["labels"]:
                    query = query.where(item.c.labels.contains(expr["labels"]) if expr["match"] == "all"
                                        else item.c.labels.overlap(expr["labels"]))
                count = conn.execute(query).scalar_one()
                label = "actionable" if op == "forge_actionable_count" else "open"
                return Evaluation(Truth.TRUE if count >= expr["at_least"] else Truth.FALSE,
                                  f"{count} {label} Forge items; threshold {expr['at_least']}")
            return Evaluation(Truth.UNKNOWN, "Unsupported condition")

        def observe(expr):
            if expr["op"] in ("queue_below", "planning_needed"):
                return observe_uncached(expr)
            from ..persistence.records import canonical
            key = ("condition", canonical(expr))
            if key not in observations:
                observations[key] = observe_uncached(expr)
            return observations[key]

        return evaluate(row["start_condition"], now, observe, events_only=events_only)

    def require_ledger_owner(self, conn, actor, assignment_id, *, allow_settled=False):
        require_project(conn, actor, project_of(conn, "assignment", assignment_id), "worker")
        if actor.kind == "agent" and live_execution(conn, actor)["assignment_id"] != assignment_id:
            # Terminal assignments cannot repair historical obligations themselves.
            if allow_settled and get(conn, "assignment", assignment_id)["status"] in TERMINAL_ASSIGNMENTS:
                require_project(conn, actor, project_of(conn, "assignment", assignment_id), "maintainer")
                return
            raise DomainError("forbidden", "An agent can change only its own obligation ledger", 403)

    def obligation(self, conn, actor: Actor, data: models.ObligationCreate) -> dict:
        self.require_ledger_owner(conn, actor, data.assignment_id)
        assignment = get(conn, "assignment", data.assignment_id)
        if assignment["status"] in TERMINAL_ASSIGNMENTS:
            raise DomainError("assignment_settled", "Reopen the assignment before editing its ledger")
        values = data.model_dump()
        if actor.kind == "agent":
            values["created_by_execution_id"] = live_execution(conn, actor)["id"]
        elif data.created_by_execution_id:
            execution = get(conn, "execution", data.created_by_execution_id)
            if execution["assignment_id"] != data.assignment_id:
                raise DomainError("scope_mismatch", "Execution belongs to another ledger", 422)
        row = create(conn, "obligation", **values,
                     number=next_number(conn, "obligation", "assignment_id", data.assignment_id))
        emit(conn, actor.id, project_of(conn, "assignment", data.assignment_id), "obligation", row, list(values))
        return row

    def resolve_obligation(self, conn, actor: Actor, identifier: UUID, data: models.ObligationResolve) -> dict:
        old = get(conn, "obligation", identifier, lock=True)
        self.require_ledger_owner(conn, actor, old["assignment_id"], allow_settled=True)
        project_id = project_of(conn, "assignment", old["assignment_id"])
        resolution = data.resolution.model_dump(mode="json")
        coordination = tables["run_coordination"]
        is_recovery = conn.execute(select(coordination.c.run_id).where(
            coordination.c.audit_obligation_id == identifier)).first() is not None
        if is_recovery and resolution["kind"] == "completed" and not resolution.get("evidence"):
            raise DomainError("coordination_evidence_required",
                "Link the repaired work, owned queue action, or event-conditioned automation before closing recovery", 422)
        if is_recovery and resolution["kind"] == "superseded" and not resolution.get("replacement_obligation_ids"):
            raise DomainError("coordination_owner_required", "Recovery needs a concrete replacement obligation", 422)
        targets = resolution.get("assignment_ids", []) + ([resolution["assignment_id"]] if "assignment_id" in resolution else [])
        for target in targets:
            owner = same_project(conn, "assignment", target, project_id)
            if owner["id"] == old["assignment_id"]:
                raise DomainError("self_delegation", "An obligation needs an independent follow-up owner", 422)
            if owner["status"] in ("failed", "cancelled"):
                raise DomainError("unavailable_owner", "Follow-up owner is failed or cancelled", 422)
            if is_recovery and owner["status"] == "completed":
                raise DomainError("unavailable_owner", "Recovery follow-up needs an unfinished owner", 422)
            if resolution["kind"] == "scheduled" and owner["status"] != "pending":
                raise DomainError("invalid_schedule", "Scheduled handling needs a pending assignment", 422)
        for replacement in resolution.get("replacement_obligation_ids", []):
            other = same_project(conn, "obligation", replacement, project_id)
            if other["id"] == identifier:
                raise DomainError("self_delegation", "An obligation cannot replace itself", 422)
        for evidence in resolution.get("evidence", []):
            same_project(conn, "repository" if evidence["kind"] in ("file", "directory") else evidence["kind"],
                         evidence.get("id", evidence.get("repository_id")), project_id)
        table = tables["obligation"]
        edges: dict[str, set[str]] = {}
        supersessions: dict[str, set[str]] = {}
        assignment, mission = tables["assignment"], tables["mission"]
        related = select(table.c.id, table.c.assignment_id, table.c.resolution).join(assignment,
            table.c.assignment_id == assignment.c.id).join(mission, assignment.c.mission_id == mission.c.id).where(
            mission.c.project_id == project_id,
            (table.c.id == identifier) | table.c.resolution["kind"].astext.in_(("delegated", "scheduled", "reconsider", "superseded")))
        for item in conn.execute(related).mappings():
            current = resolution if item["id"] == identifier else item["resolution"] or {}
            values = current.get("assignment_ids", []) + ([current["assignment_id"]] if "assignment_id" in current else [])
            edges.setdefault(str(item["assignment_id"]), set()).update(map(str, values))
            supersessions[str(item["id"])] = set(map(str, current.get("replacement_obligation_ids", [])))
        reject_cycle(edges)
        reject_cycle(supersessions)
        row = change(conn, "obligation", identifier, data.expected_revision,
                     status=data.status, resolution=resolution)
        emit(conn, actor.id, project_id, "obligation", row, ["status", "resolution"], previous=old["status"])
        return row

    def completion_findings(self, conn, assignment_id: UUID) -> list[str]:
        findings = []
        table = tables["obligation"]
        assignment = get(conn, "assignment", assignment_id)
        demand = tables["review_demand"]
        if conn.execute(select(demand.c.forge_item_id).where(demand.c.assignment_id == assignment_id,
                demand.c.handled_generation < demand.c.owner_generation).limit(1)).first():
            findings.append("Deliver the review decision and settle_review for this session's assigned generation.")
        orchestrator = "orchestrator" in (assignment.get("functions") or [])
        open_query = select(func.count()).select_from(table).where(
            table.c.assignment_id == assignment_id, table.c.status == "open")
        if orchestrator:
            # The host settles its generated episode record. Explicit control
            # work and recovery blockers still require an accountable outcome.
            open_query = open_query.where(~((table.c.number == 1) & table.c.created_by_execution_id.is_(None)))
        open_count = conn.execute(open_query).scalar_one()
        if open_count:
            findings.append(f"Account for the {open_count} open obligations by doing the work or recording durable handling.")
        root = conn.execute(select(table).where(table.c.assignment_id == assignment_id, table.c.number == 1)).mappings().first()
        resolution = (root["resolution"] or {}) if root else {}
        targets = resolution.get("assignment_ids", []) + ([resolution["assignment_id"]] if resolution.get("assignment_id") else [])
        if root and root["status"] == "handled" and len(set(targets)) == 1:
            own = get(conn, "assignment", assignment_id)
            other = get(conn, "assignment", targets[0])
            if other["status"] in ("pending", "running") and own["role"] == other["role"]:
                same_goal = own["mission_id"] == other["mission_id"]
                if not same_goal:
                    original = get(conn, "mission", own["mission_id"])["objective"]
                    delegated = get(conn, "mission", other["mission_id"])["objective"]
                    same_goal = " ".join(original.split()) == " ".join(delegated.split())
                if same_goal:
                    findings.append("The mission was handed unchanged to one unfinished assignment. "
                        "Continue this context, narrow the delegated mission while retaining your own work, "
                        "or decompose the work into independent scopes. Publish your bounded deliverable "
                        "and give dependent follow-up work its own scoped queued owner.")
        return findings + self.settlement_findings(conn, assignment_id)

    @staticmethod
    def assignment_deliveries(assignment_id: UUID):
        outbox, principal, execution = (tables[name] for name in ("outbox_operation", "principal", "execution"))
        return select(outbox).join(principal, outbox.c.actor_principal_id == principal.c.id).join(
            execution, principal.c.execution_id == execution.c.id).where(
                execution.c.assignment_id == assignment_id, outbox.c.kind.in_(EXTERNAL_DELIVERIES))

    def pending_deliveries(self, conn, assignment_id: UUID) -> bool:
        outbox = tables["outbox_operation"]
        return conn.execute(self.assignment_deliveries(assignment_id).with_only_columns(outbox.c.id).where(
            outbox.c.status.in_(("pending", "running", "uncertain"))).limit(1)).first() is not None

    def settlement_findings(self, conn, assignment_id: UUID, *, include_pending_deliveries: bool = True) -> list[str]:
        findings = []
        thread, request = tables["provider_thread"], tables["provider_request"]
        unsettled = conn.execute(select(func.count()).select_from(request.join(thread,
            request.c.provider_thread_id == thread.c.id)).where(
            thread.c.assignment_id == assignment_id,
            request.c.status.in_(("pending", "submitted", "running", "uncertain")))).scalar_one()
        if unsettled:
            findings.append(f"Reconcile {unsettled} outstanding provider requests before completion.")
        notification = tables["notification"]
        control = conn.execute(select(func.count()).select_from(notification).where(
            notification.c.assignment_id == assignment_id, notification.c.urgency == "control",
            notification.c.disposition == "pending")).scalar_one()
        if control:
            findings.append(f"Handle {control} pending control notifications.")
        outbox = tables["outbox_operation"]
        states = ("pending", "running", "uncertain", "failed") if include_pending_deliveries else ("failed",)
        pending = self.assignment_deliveries(assignment_id).with_only_columns(outbox.c.id).where(outbox.c.status.in_(states))
        deliveries = conn.execute(select(func.count()).select_from(pending.subquery())).scalar_one()
        if deliveries:
            findings.append(f"Reconcile {deliveries} outstanding Forge or Zulip deliveries before completion; inspect their receipts and failures.")
        from sqlalchemy import cast, Text
        goal_updates = conn.execute(select(func.count()).select_from(outbox).where(outbox.c.kind == "goal_update",
            outbox.c.status.in_(("pending", "running")),
            outbox.c.payload["provider_thread_id"].astext.in_(select(cast(thread.c.id, Text)).where(
                thread.c.assignment_id == assignment_id)))).scalar_one()
        if goal_updates:
            findings.append("Read and follow the updated mission or roadmap baseline in the continuation input. "
                "Successful provider completion automatically acknowledges that input's pinned revision. "
                "Do not PATCH the mission or rewrite delegation_note merely to acknowledge it; "
                "that creates another goal update. Make a mission edit only for an actual contract or guidance change.")
        return findings

    def progress_stalled(self, conn, assignment_id: UUID, request_window: int) -> bool:
        from ..execution.intent_reconciliation import JOURNAL_BLOCKER_PREFIX, INTENT_REPAIR_PREFIX
        request, thread, obligation = (tables[name] for name in ("provider_request", "provider_thread", "obligation"))
        # A delivered or failed external operation supplies new actionable evidence.
        # Waiting turns before that receipt must not exhaust the resumed context.
        outbox = tables["outbox_operation"]
        settled = conn.execute(self.assignment_deliveries(assignment_id).with_only_columns(outbox.c.updated_at).where(
            outbox.c.status.in_(("completed", "failed", "cancelled")),
            # Routine outgoing narration is not progress. Failures still need repair.
            (outbox.c.kind.in_(("forge_change", "forge_edit", "forge_create", "forge_merge", "forge_review"))
             | outbox.c.status.in_(("failed", "cancelled")))).order_by(
                outbox.c.updated_at.desc()).limit(1)).scalar_one_or_none()
        dates = list(conn.execute(select(request.c.created_at).select_from(request.join(thread,
            request.c.provider_thread_id == thread.c.id)).where(thread.c.assignment_id == assignment_id,
            thread.c.kind == "primary", request.c.status.in_(("completed", "failed", "interrupted")),
            *([request.c.created_at > settled] if settled else []))
            .order_by(request.c.created_at.desc()).limit(request_window)).scalars())
        if len(dates) < request_window:
            return False
        progress = conn.execute(select(obligation.c.id).where(obligation.c.assignment_id == assignment_id,
            obligation.c.status != "open", obligation.c.updated_at >= dates[-1],
            ~obligation.c.description.startswith(JOURNAL_BLOCKER_PREFIX),
            ~obligation.c.description.startswith(INTENT_REPAIR_PREFIX)).limit(1)).first()
        return progress is None

    def context(self, conn, actor: Actor, assignment_id: UUID, *, view: str = "brief") -> dict:
        from ..execution.coordination import summary as coordination_summary
        from ..execution.notifications import control_summary
        assignment = get(conn, "assignment", assignment_id)
        require_project(conn, actor, project_of(conn, "assignment", assignment_id))
        if view == "operations":
            from ..instructions.prompts import operations_context
            return operations_context(conn, self, assignment)
        if "orchestrator" in (assignment.get("functions") or []) and view in ("brief", "full"):
            from ..instructions.prompts import orchestrator_context
            return orchestrator_context(conn, self, assignment)
        result = {"assignment": assignment, "mission": get(conn, "mission", assignment["mission_id"]),
                  "run": get(conn, "run", assignment["run_id"]), "collections": {}}
        result["control_notices"] = control_summary(conn, assignment_id)
        result["coordination"] = coordination_summary(conn, self, result["run"])
        from ..execution.coordination_memory import memory
        result["coordination"]["memory"] = memory(conn, result["run"]["id"], assignment_id=assignment_id)
        if view == "brief":
            from ..execution.context_briefing import build
            return build(conn, self, assignment, result["mission"], result["run"],
                         result["control_notices"], result["coordination"])
        if view != "full":
            raise DomainError("invalid_context_view", "Choose brief, full or operations context", 422)
        thread, request = tables["provider_thread"], tables["provider_request"]
        current = conn.execute(select(request).join(thread, request.c.provider_thread_id == thread.c.id).where(
            thread.c.assignment_id == assignment_id, thread.c.kind == "primary",
            request.c.status.in_(("pending", "submitted", "running"))).order_by(request.c.created_at.desc()).limit(1)).mappings().first()
        result["current_provider_request"] = dict(current) if current else None
        for name in ("obligation", "activity", "provider_thread", "execution", "notification"):
            table = tables[name]
            query = select(table).where(table.c.assignment_id == assignment_id)
            if name == "obligation":
                query = query.order_by((table.c.status == "open").desc())
            if name == "notification":
                query = query.order_by((table.c.disposition == "pending").desc(), (table.c.urgency == "control").desc())
            query = query.order_by(table.c.created_at.desc()).limit(100)
            collection = {"activity": "activity"}.get(name, name + "s")
            result[collection] = [dict(row) for row in conn.execute(query).mappings()]
            total = conn.execute(select(func.count()).select_from(table).where(table.c.assignment_id == assignment_id)).scalar_one()
            result["collections"][collection] = {"total": total, "truncated": total > len(result[collection]),
                "list_url": f"/api/v3/records/{name}?assignment_id={assignment_id}"}
        return result
