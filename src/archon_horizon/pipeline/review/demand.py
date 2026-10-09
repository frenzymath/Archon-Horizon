"""One durable maintenance owner per Forge item, with coalesced review generations."""

from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from .. import models
from ..auth import live_execution, require_project
from ..errors import DomainError
from ..persistence.records import change, create, emit, get, project_of
from ..persistence.schema import tables
from ..instructions.templates import template

LIVE = ("pending", "running", "stopping")


def fingerprint(item):
    # Poll timestamps are deliberately absent: unchanged observations do not
    # create review rounds. Explicit requests can advance the generation.
    return hashlib.sha256(json.dumps([str(item["repository_id"]), item["remote_number"],
        item["kind"], item["head_commit_oid"], item["target_branch"], item["status"]]).encode()).hexdigest()


def observe(conn, run, item, now, *, note=None, explicit=False):
    table = tables["review_demand"]
    old = conn.execute(select(table).where(table.c.forge_item_id == item["id"]).with_for_update()).mappings().first()
    label = "awaiting-review" in item["labels"]
    attention = item["status"] == "open" and (explicit or label)
    if old is None:
        if not attention:
            return None
        conn.execute(insert(table).values(forge_item_id=item["id"], run_id=run["id"], fingerprint=fingerprint(item),
            attention=True, observed_label=label, requested_at=now, note=note).on_conflict_do_nothing())
    else:
        if old["generation"] > old["handled_generation"] and item["status"] == "open":
            attention = True  # An undelivered label cannot cancel durable demand.
        if old["run_id"] != run["id"]:
            previous = get(conn, "run", old["run_id"])
            if previous["status"] not in ("completed", "cancelled"):
                return None
            # A deliberate new run may inherit outstanding review, but never
            # overlap a still-running process from the previous owner.
            execution = tables["execution"]
            if old["assignment_id"] and conn.execute(select(execution.c.id).where(
                    execution.c.assignment_id == old["assignment_id"],
                    execution.c.stop_confirmed_at.is_(None)).limit(1)).first():
                return None
        changed = fingerprint(item) != old["fingerprint"]
        renewed = attention and (explicit or changed or not old["observed_label"] and label)
        # A settled round with a still-visible label is delivery lag, not a new
        # request. Observe removal before interpreting a subsequent rising edge.
        attention = item["status"] == "open" and (renewed or old["generation"] > old["handled_generation"])
        values = {"attention": attention, "observed_label": label}
        if item["status"] != "open":
            values["handled_generation"] = old["generation"]
        if attention and old["run_id"] != run["id"]:
            values.update(run_id=run["id"], assignment_id=None, owner_generation=0)
        if renewed:
            values.update(generation=old["generation"] + 1, fingerprint=fingerprint(item),
                requested_at=now, note=note, run_id=run["id"])
        conn.execute(update(table).where(table.c.forge_item_id == item["id"]).values(**values))
    return dict(conn.execute(select(table).where(table.c.forge_item_id == item["id"])).mappings().one())


def request(conn, actor, service, item_id, note, *, run_id=None):
    item = get(conn, "forge_item", item_id)
    project_id = project_of(conn, "forge_item", item_id)
    require_project(conn, actor, project_id, "worker")
    execution = live_execution(conn, actor) if actor.kind == "agent" else None
    run_id = run_id or (execution["run_id"] if execution else item["origin_run_id"])
    if not run_id:
        raise DomainError("review_origin_required", "Select run_id for this human-created review request", 422)
    run = get(conn, "run", run_id)
    repository = get(conn, "repository", item["repository_id"])
    if project_of(conn, "run", run["id"]) != project_id or repository["purpose"] not in ("knowledge", "library"):
        raise DomainError("scope_mismatch", "Review requires a roadmap or library in this objective's project", 403)
    if item["origin_run_id"] and item["origin_run_id"] != run["id"]:
        origin = get(conn, "run", item["origin_run_id"])
        if origin["status"] not in ("completed", "cancelled"):
            raise DomainError("scope_mismatch", "The item belongs to another live objective", 403)
    # A repair may outlive its original phase. Explicit run selection binds
    # ownership while review_phase retains the item's applicable review gate.
    if run.get("orchestration") != "objective" or run["status"] != "active" or item["status"] != "open":
        raise DomainError("review_not_active", "Review requests need an open item and active objective", 409)
    if execution and execution["run_id"] != run["id"]:
        raise DomainError("scope_mismatch", "The item belongs to another objective", 403)
    old = conn.execute(select(tables["review_demand"]).where(tables["review_demand"].c.forge_item_id == item_id)).mappings().first()
    if old and old["fingerprint"] == fingerprint(item) and not note.strip():
        raise DomainError("review_reason_required", "Same-head review requests need the new evidence or decision required", 422)
    result = observe(conn, run, item, datetime.now(timezone.utc), note=note, explicit=True)
    if result is None:
        raise DomainError("review_owner_retained", "Settle the previous objective and confirm its review owner's physical stop", 409)
    # The local intent is authoritative. The label is a recoverable projection,
    # not a prerequisite that an eventually consistent remote poll can erase.
    create(conn, "outbox_operation", project_id=project_id, actor_principal_id=actor.id,
        kind="forge_label", schema_version=1, idempotency_key=f"review-request:{item_id}:{result['generation']}",
        payload={"forge_item_id": str(item_id), "add": ["awaiting-review"], "remove": [], "review_generation": result["generation"]})
    emit(conn, actor.id, project_id, "forge_item", item, ["review_demand"],
        note=f"Requested review generation {result['generation']} in objective run {run['id']}: {note}")
    return result


def reconcile(scheduler, conn, actor, now):
    from ..execution.objectives import child_mission
    from ..execution.queue_policies import validate_enqueue
    item, repo, run_table = tables["forge_item"], tables["repository"], tables["run"]
    created = 0
    runs = list(conn.execute(select(run_table).where(run_table.c.orchestration == "objective", run_table.c.status == "active")).mappings())
    for raw in runs:
        run = dict(raw)
        if run.get("pending_phase"):
            continue
        root = get(conn, "mission", run["mission_id"])
        # Human-created labelled items have no launch provenance. Attribute
        # them automatically only when this project has one active objective;
        # otherwise an explicit request must select the objective.
        root_table = tables["mission"]
        active_objectives = conn.execute(select(func.count()).select_from(run_table.join(root_table,
            root_table.c.id == run_table.c.mission_id)).where(run_table.c.orchestration == "objective",
                run_table.c.status == "active", root_table.c.project_id == root["project_id"])).scalar_one()
        external = (item.c.origin_run_id.is_(None) & item.c.labels.contains(["awaiting-review"]) &
                    or_(item.c.review_phase.is_(None), item.c.review_phase == run["phase"]["kind"])) if active_objectives == 1 else False
        candidates = conn.execute(select(item).join(repo).where(repo.c.project_id == root["project_id"],
            repo.c.purpose.in_(("knowledge", "library")),
            or_(external, item.c.origin_run_id == run["id"], item.c.id.in_(select(tables["review_demand"].c.forge_item_id).where(
                tables["review_demand"].c.run_id == run["id"]))))
            .order_by(item.c.observed_at, item.c.id).offset(getattr(scheduler, "_review_offsets", {}).get(run["id"], 0)).limit(scheduler.CLAIM_SCAN_LIMIT)).mappings()
        page = list(candidates)
        if not hasattr(scheduler, "_review_offsets"):
            scheduler._review_offsets = {}
        scheduler._review_offsets[run["id"]] = scheduler._review_offsets.get(run["id"], 0) + len(page) if len(page) == scheduler.CLAIM_SCAN_LIMIT else 0
        for raw_item in page:
            current = dict(raw_item)
            if not current["review_phase"]:
                current = change(conn, "forge_item", current["id"], review_phase=run["phase"]["kind"])
            demand = observe(conn, run, current, now)
            if demand and current["status"] != "open" and demand["assignment_id"]:
                owner = get(conn, "assignment", demand["assignment_id"])
                if owner["status"] in LIVE:
                    scheduler.cancel(conn, scheduler.system_actor(conn), owner,
                        "Review item closed; request settled without a new review decision")
            if not demand or demand["handled_generation"] >= demand["generation"] or current["status"] != "open":
                continue
            owner = get(conn, "assignment", demand["assignment_id"]) if demand["assignment_id"] else None
            if owner:
                if owner["status"] in LIVE or owner["status"] == "failed":
                    # Failure retains the owner and circuit state. A new label
                    # cannot buy a fresh retry budget with another assignment.
                    continue
                if owner["status"] == "completed":
                    if demand["owner_generation"] >= demand["generation"]:
                        continue
                elif owner["status"] == "cancelled":
                    # Reopening a settled item is a new decision. Cancelling
                    # an unresolved owner or toggling its label cannot reset
                    # recovery history or overlap an unconfirmed process.
                    execution = tables["execution"]
                    if (demand["handled_generation"] < demand["owner_generation"] or
                            demand["generation"] <= demand["owner_generation"] or
                            conn.execute(select(execution.c.id).where(execution.c.assignment_id == owner["id"],
                                execution.c.stop_confirmed_at.is_(None)).limit(1)).first()):
                        continue
            data = models.AssignmentCreate(run_id=run["id"], mission_id=root["id"], role="maintainer", category="maintenance")
            try:
                validate_enqueue(conn, run, data)
            except DomainError as error:
                if error.code == "queue_full":
                    break
                raise
            generation = demand["generation"]
            mission = child_mission(scheduler, conn, run, f"Review {current['kind']} #{current['remote_number']}",
                template("maintenance-mission", generation=generation, kind=current["kind"],
                    number=current["remote_number"], title=current["title"]),
                ["The item has an attributable decision and any repair has a durable owner."])
            instructions = template("maintenance-instructions", item_id=current["id"], generation=generation,
                head=current["head_commit_oid"] or "issue", target=current["target_branch"] or "none",
                request=demand["note"] or "awaiting-review")
            assignment = scheduler.service.assignment(conn, scheduler.system_actor(conn), data.model_copy(update={
                "mission_id": mission["id"], "instructions": instructions}), internal=True)
            change(conn, "assignment", assignment["id"], recurrence_key=f"review:{current['id']}")
            conn.execute(update(tables["review_demand"]).where(tables["review_demand"].c.forge_item_id == current["id"])
                .values(assignment_id=assignment["id"], owner_generation=generation))
            created += 1
    return created


def settle(conn, actor, service, item_id, generation, note):
    require_project(conn, actor, project_of(conn, "forge_item", item_id), "maintainer")
    table = tables["review_demand"]
    demand = conn.execute(select(table).where(table.c.forge_item_id == item_id).with_for_update()).mappings().first()
    if not demand or generation > demand["generation"]:
        raise DomainError("review_generation_changed", "Refresh the review demand before settling it", 409)
    if actor.kind == "agent" and live_execution(conn, actor)["assignment_id"] != demand["assignment_id"]:
        raise DomainError("review_owner_required", "Only the current maintenance owner may settle this round", 403)
    if actor.kind == "agent" and generation != demand["owner_generation"]:
        raise DomainError("review_generation_changed", "Settle only the generation assigned to this session", 409)
    conn.execute(update(table).where(table.c.forge_item_id == item_id).values(
        handled_generation=max(generation, demand["handled_generation"])))
    if generation == demand["generation"]:
        # Connector delivery checks the generation again. A newer request may
        # arrive while this operation waits in the durable outbox.
        create(conn, "outbox_operation", project_id=project_of(conn, "forge_item", item_id), actor_principal_id=actor.id,
            kind="forge_label", schema_version=1, idempotency_key=f"review-settle:{item_id}:{generation}",
            payload={"forge_item_id": str(item_id), "add": [], "remove": ["awaiting-review"],
                     "review_generation": generation})
    emit(conn, actor.id, project_of(conn, "forge_item", item_id), "forge_item", get(conn, "forge_item", item_id),
        ["review_demand"], note=f"Settled review generation {generation}: {note}")
    return {"forge_item_id": str(item_id), "generation": generation, "note": note}


def label_projection(conn, item_id, payload):
    """Rebase delayed label deliveries onto the authoritative review generation."""
    demand = conn.execute(select(tables["review_demand"]).where(
        tables["review_demand"].c.forge_item_id == item_id)).mappings().first()
    if not demand or "review_generation" not in payload:
        return payload
    wants = demand["attention"] and demand["generation"] > demand["handled_generation"]
    return {**payload,
        "add": [v for v in payload["add"] if v != "awaiting-review"] + (["awaiting-review"] if wants else []),
        "remove": [v for v in payload["remove"] if v != "awaiting-review"] + ([] if wants else ["awaiting-review"])}


def repair_label(conn, item, actor_id, operation_id):
    """Repair a generation change racing a remote label call without a new owner."""
    demand = conn.execute(select(tables["review_demand"]).where(
        tables["review_demand"].c.forge_item_id == item["id"])).mappings().first()
    if not demand:
        return
    wants = item["status"] == "open" and demand["attention"] and demand["generation"] > demand["handled_generation"]
    if wants == ("awaiting-review" in item["labels"]):
        return
    create(conn, "outbox_operation", project_id=project_of(conn, "forge_item", item["id"]),
        actor_principal_id=actor_id, kind="forge_label", schema_version=1,
        idempotency_key=f"review-projection:{operation_id}:{demand['generation']}",
        payload={"forge_item_id": str(item["id"]), "review_generation": demand["generation"],
                 "add": ["awaiting-review"] if wants else [], "remove": [] if wants else ["awaiting-review"]})
