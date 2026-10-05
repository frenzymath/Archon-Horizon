"""Run-wide handoffs and evidence-based coordination recovery.

Provider turns, queue churn, polling timestamps and routine messages are not
new evidence. A recovery episode is owned by an ordinary planner obligation;
its decisions and follow-up owners survive the provider session.
"""

import hashlib
from datetime import timedelta
from uuid import UUID

from sqlalchemy import Text, cast, func, or_, select, update

from .records import canonical, change, create, emit, get, next_number, project_of
from .schema import tables

ACTIVE = ("pending", "running", "stopping")
NOTE_PREFIX = "Coordination: "


def fingerprint(publications, reviews, items, outcomes):
    """Stable across duplicate reports and observation/assignment identifiers."""
    def unique(rows):
        return sorted({canonical(row).decode() for row in rows})
    return hashlib.sha256(canonical([unique(rows) for rows in
        (publications, reviews, items, outcomes)])).hexdigest()


def run_items(run_id):
    """Include inherited PRs with a concrete amendment or review owner in this run."""
    item, operation, work, obligation, assignment = (tables[name] for name in
        ("forge_item", "outbox_operation", "review_work", "obligation", "assignment"))
    amended = select(operation.c.payload["forge_item_id"].astext).where(
        operation.c.kind == "forge_change", operation.c.status == "completed",
        operation.c.payload["origin_run_id"].astext == str(run_id))
    reviewed = select(work.c.forge_item_id).select_from(work.join(obligation,
        work.c.obligation_id == obligation.c.id).join(assignment,
        obligation.c.assignment_id == assignment.c.id)).where(
            assignment.c.run_id == run_id)
    return or_(item.c.origin_run_id == run_id, cast(item.c.id, Text).in_(amended), item.c.id.in_(reviewed))


def frontier(conn, run_id):
    a, p, artifact, item, review, obligation = (tables[name] for name in
        ("assignment", "publication", "artifact", "forge_item", "forge_review", "obligation"))
    from .root_maintenance import NAME
    automation = tables["automation"]
    root_automations = select(automation.c.id).where(automation.c.name == NAME)
    publications = conn.execute(select(artifact.c.content).select_from(p.join(artifact).join(
        a, p.c.requested_by_assignment_id == a.c.id)).where(a.c.run_id == run_id,
        ~a.c.functions.contains(["planner"]),
        or_(a.c.automation_id.is_(None), a.c.automation_id.not_in(root_automations)),
        p.c.status == "verified", artifact.c.kind == "commit").distinct()).scalars()
    items = list(conn.execute(select(item.c.id, item.c.head_commit_oid, item.c.status).where(
        run_items(run_id))).mappings())
    identity = func.coalesce(cast(review.c.reviewer_descriptor_id, Text),
                             review.c.reviewer_remote_id)
    # A later approval may resolve an objection on the very same commit.
    reviews = conn.execute(select(review.c.forge_item_id, identity.label("reviewer"),
        review.c.verdict, func.md5(review.c.summary).label("summary"))
        .join(item, review.c.forge_item_id == item.c.id).where(run_items(run_id),
            review.c.commit_oid == item.c.head_commit_oid,
            review.c.verdict.in_(("approved", "changes_requested")),
            ~review.c.summary.contains("**Historical reviewer feedback:**"))
        .distinct(review.c.forge_item_id, identity).order_by(review.c.forge_item_id, identity,
            review.c.observed_at.desc(), review.c.created_at.desc(), review.c.id.desc())).mappings()
    # A failed worker or a substantive handoff merits reconsideration, even
    # without a new commit. Repeating the same outcome under a new ID does not.
    outcomes = conn.execute(select(a.c.status,
        func.left(obligation.c.description, 1000).label("task"), obligation.c.resolution)
        .join(obligation, obligation.c.assignment_id == a.c.id).where(a.c.run_id == run_id,
            a.c.automation_id.is_(None),
            a.c.status.in_(("completed", "failed", "cancelled")),
            obligation.c.number == 1)).mappings()
    return fingerprint(list(publications), [dict(r) for r in reviews], [dict(r) for r in items],
                       [dict(r) for r in outcomes])


def state(conn, run_id):
    t = tables["run_coordination"]
    row = conn.execute(select(t).where(t.c.run_id == run_id)).mappings().first()
    return dict(row) if row else None


def episode_status(watch, audit, owner):
    if not audit or watch["audit_frontier_hash"] != watch["frontier_hash"]:
        return "observing"
    if owner["status"] in ("failed", "cancelled", "completed") and audit["status"] == "open":
        return "owner_lost"
    return "review_needed" if audit["status"] == "open" else "awaiting_result"


def memory(conn, run_id, *, assignment_id=None, max_bytes=12000):
    """Bounded evidence excerpts, with explicit links to omitted history."""
    from .context_briefing import columns, record, size
    a, o, mission = (tables[name] for name in ("assignment", "obligation", "mission"))
    af = ("id", "number", "mission_id", "role", "functions", "status", "status_note",
          "start_condition", "not_before", "retry_at", "finished_at")
    recent = conn.execute(select(*columns(a, af), func.left(mission.c.title, 240).label("title"))
        .join(mission).where(a.c.run_id == run_id, a.c.status.in_(("completed", "failed", "cancelled")),
            or_(a.c.started_at.is_not(None), a.c.automation_id.is_(None)))
        .order_by(a.c.finished_at.desc().nullslast(), a.c.number.desc()).limit(6)).mappings()
    recent = [dict(row) for row in recent]
    roots = {row["assignment_id"]: dict(row) for row in conn.execute(select(*columns(o,
        ("id", "assignment_id", "status", "resolution"))).where(o.c.assignment_id.in_([r["id"] for r in recent]),
        o.c.number == 1)).mappings()}
    handoffs = []
    for row in recent:
        entry = record(row, "assignment", (*af, "title"), text_limit=400)
        if row["id"] in roots:
            entry["outcome"] = record(roots[row["id"]], "obligation", ("id", "status", "resolution"))
        handoffs.append(entry)
    owners = [record(dict(row), "assignment", (*af, "title"), text_limit=400) for row in conn.execute(
        select(*columns(a, af), func.left(mission.c.title, 240).label("title"))
        .join(mission).where(a.c.run_id == run_id, a.c.status.in_(ACTIVE),
            a.c.id != assignment_id if assignment_id else True)
        .order_by(a.c.queue_rank, a.c.id).limit(12)).mappings()]
    fields = ("id", "assignment_id", "kind", "description", "status", "resolution")
    unresolved = [record(dict(row), "obligation", (*fields, "owner_status"), text_limit=500)
        for row in conn.execute(select(*columns(o, fields), a.c.status.label("owner_status"))
            .join(a, o.c.assignment_id == a.c.id).where(a.c.run_id == run_id,
                o.c.assignment_id != assignment_id if assignment_id else True,
                or_(o.c.number > 1, o.c.kind.in_(("blocker", "decision")), o.c.status == "handled"),
                or_(o.c.status == "open", o.c.status == "handled"))
            .order_by((o.c.kind == "blocker").desc(), o.c.updated_at.desc(), o.c.id).limit(12)).mappings()]
    target_ids = set()
    for entry in unresolved:
        resolution = entry.get("resolution") or {}
        target_ids.update(UUID(value) for value in resolution.get("assignment_ids", []))
        if resolution.get("assignment_id"):
            target_ids.add(UUID(resolution["assignment_id"]))
    targets = {str(row["id"]): record(dict(row), "assignment", af, text_limit=400)
        for row in conn.execute(select(*columns(a, af)).where(a.c.id.in_(target_ids))).mappings()}
    for entry in unresolved:
        resolution = entry.get("resolution") or {}
        ids = resolution.get("assignment_ids", []) + ([resolution["assignment_id"]] if resolution.get("assignment_id") else [])
        entry["follow_up_owners"] = [targets[key] for key in ids if key in targets]
    item, review = tables["forge_item"], tables["forge_review"]
    review_fields = ("id", "forge_item_id", "reviewer_remote_id", "verdict", "summary", "commit_oid")
    reviews = [record(dict(row), "forge_review", review_fields, text_limit=600)
        for row in conn.execute(select(*columns(review, review_fields)).join(item,
            review.c.forge_item_id == item.c.id).where(run_items(run_id),
                item.c.status == "open", review.c.commit_oid == item.c.head_commit_oid)
            .order_by(review.c.observed_at.desc(), review.c.id.desc()).limit(6)).mappings()]
    watch = state(conn, run_id)
    result = {"recent_handoffs": handoffs, "owners": owners, "unresolved": unresolved,
        "current_head_reviews": reviews,
        "history_url": f"/api/v3/records/assignment?run_id={run_id}",
        "obligations_url": f"/api/v3/records/obligation?run_id={run_id}",
        "bounded": True, "note": "Excerpts only. Read linked outcomes and target assignments before replacing an owner."}
    if watch:
        audit = get(conn, "obligation", watch["audit_obligation_id"]) if watch["audit_obligation_id"] else None
        owner = get(conn, "assignment", audit["assignment_id"]) if audit else None
        result["episode"] = {"status": episode_status(watch, audit, owner),
            "last_evidence_change_at": watch["last_progress_at"], "checked_at": watch["checked_at"],
            "fingerprint": watch["frontier_hash"],
            "audit": record(audit, "obligation", ("id", "assignment_id", "status", "description", "resolution")) if audit else None}
    for key in ("owners", "recent_handoffs", "current_head_reviews", "unresolved"):
        while result[key] and size(result) > max_bytes:
            result[key].pop()
            result["truncated"] = True
    return result


def follow_up_status(conn, run_id, audit, now, *, timeout_seconds=1800):
    """Inspect the promised owners without treating their activity as a result."""
    if not audit or audit["status"] == "open":
        return {"status": "review_needed" if audit else "observing", "deadline": None,
                "owners": [], "issues": []}
    resolution = audit["resolution"] or {}
    deadline = audit["updated_at"] + timedelta(seconds=timeout_seconds)
    assignment_ids = set(resolution.get("assignment_ids", []))
    if resolution.get("assignment_id"):
        assignment_ids.add(resolution["assignment_id"])
    automation_ids = set()
    obligation_ids = set(resolution.get("replacement_obligation_ids", []))
    # Explicit delegation names the promised owners. Evidence can describe
    # previous work, including completed authors, without delegating to them.
    for evidence in (() if assignment_ids else resolution.get("evidence", [])):
        identifier = evidence.get("id")
        if identifier and evidence["kind"] == "assignment":
            if get(conn, "assignment", identifier)["status"] in ACTIVE:
                assignment_ids.add(identifier)
        elif identifier and evidence["kind"] == "automation":
            automation_ids.add(identifier)
        elif identifier and evidence["kind"] == "obligation":
            obligation_ids.add(identifier)
    issues = []
    pending = [(str(identifier), frozenset()) for identifier in sorted(obligation_ids)]
    visited = set()
    while pending:
        identifier, ancestors = pending.pop()
        if identifier in ancestors:
            issues.append("Recovery obligation follow-up contains a cycle")
            continue
        if identifier in visited:
            continue
        if len(visited) >= 64:
            issues.append("Recovery obligation follow-up exceeds 64 linked obligations")
            break
        visited.add(identifier)
        obligation = get(conn, "obligation", identifier)
        if obligation["status"] == "open":
            assignment_ids.add(str(obligation["assignment_id"]))
        elif obligation["status"] in ("handled", "superseded"):
            successor = obligation["resolution"] or {}
            assignment_ids.update(successor.get("assignment_ids", []))
            if successor.get("assignment_id"):
                assignment_ids.add(successor["assignment_id"])
            pending.extend((str(value), ancestors | {identifier})
                for value in successor.get("replacement_obligation_ids", []))
    for identifier in automation_ids:
        rule = get(conn, "automation", identifier)
        if rule["run_id"] != run_id or "orchestrator" in rule["functions"]:
            issues.append("Recovery follow-up points to another run or a supervisory automation")
        elif not rule["enabled"]:
            issues.append(f"Recovery automation {rule['name']} is disabled")
        a = tables["assignment"]
        assignment_ids.update(str(value) for value in conn.execute(select(a.c.id).where(
            a.c.automation_id == rule["id"], a.c.status.in_(ACTIVE))).scalars())
    from .admission import reason, run_usage
    campaign = get(conn, "run", run_id)
    usage = run_usage(conn, campaign) if assignment_ids else None
    owners = []
    for identifier in sorted(assignment_ids):
        owner = get(conn, "assignment", identifier)
        blocked = None
        if owner["run_id"] != run_id or "orchestrator" in owner["functions"]:
            blocked = "Follow-up owner belongs to another run or is a supervisor"
        elif owner["id"] == audit["assignment_id"]:
            blocked = "The diagnostic owner is its own follow-up"
        elif owner["status"] in ("failed", "cancelled", "stopping"):
            blocked = f"Follow-up owner is {owner['status']}"
        elif owner["status"] == "completed":
            blocked = "Follow-up owner completed without changed work evidence"
        elif owner["status"] == "pending":
            blocked = reason(campaign, usage, now, already_started=owner["started_at"] is not None)
            if owner["expires_at"] and owner["expires_at"] <= now:
                blocked = "Follow-up owner's start window expired"
            if owner["automation_id"] and not get(conn, "automation", owner["automation_id"])["enabled"]:
                blocked = "Follow-up owner's automation is disabled"
        owners.append({"id": owner["id"], "number": owner["number"], "status": owner["status"],
                       "role": owner["role"], "functions": owner["functions"], "blocker": blocked})
        if blocked:
            issues.append(f"A{owner['number']}: {blocked}")
    if not issues and now >= deadline:
        issues.append("Recovery follow-up deadline passed without changed work evidence")
    return {"status": "attention_required" if issues else "awaiting_result", "deadline": deadline,
            "owners": owners, "issues": issues}


def awaiting_evidence(conn, run_id, *, candidate_id=None, now=None, timeout_seconds=1800):
    watch = state(conn, run_id)
    if not watch or not watch["audit_obligation_id"] or watch["audit_frontier_hash"] != watch["frontier_hash"]:
        return False
    audit = get(conn, "obligation", watch["audit_obligation_id"])
    resolution = audit["resolution"] or {}
    owners = resolution.get("assignment_ids", []) + ([resolution["assignment_id"]] if resolution.get("assignment_id") else [])
    if candidate_id and str(candidate_id) in owners:
        return False
    now = now or conn.execute(select(func.now())).scalar_one()
    return follow_up_status(conn, run_id, audit, now, timeout_seconds=timeout_seconds)["status"] == "awaiting_result"


def recovery_due(watch, now, passes, config):
    return passes >= config.coordination_repeat_passes or (
        now - watch["last_progress_at"]).total_seconds() >= config.coordination_stall_seconds


def reconcile(scheduler, conn, actor, now):
    """One owned diagnostic episode per unchanged frontier, never a polling agent."""
    from .conditions import Evaluation, Truth, evaluate
    a, run, automation, watch_table = (tables[name] for name in
        ("assignment", "run", "automation", "run_coordination"))
    changed = 0
    from .root_maintenance import NAME
    root_owned = select(automation.c.id).where(automation.c.run_id == run.c.id,
        automation.c.name == NAME).exists()
    for campaign in list(conn.execute(select(run).where(run.c.status == "active", ~root_owned)).mappings()):
        watch = state(conn, campaign["id"])
        if watch and (now - watch["checked_at"]).total_seconds() < 60:
            continue
        observed = frontier(conn, campaign["id"])
        values = {"frontier_hash": observed, "checked_at": now}
        if watch is None:
            conn.execute(watch_table.insert().values(run_id=campaign["id"], last_progress_at=now, **values))
            continue
        if observed != watch["frontier_hash"]:
            values["last_progress_at"] = now
            if (campaign["status_note"] or "").startswith(NOTE_PREFIX):
                change(conn, "run", campaign["id"], status_note=None)
        conn.execute(update(watch_table).where(watch_table.c.run_id == campaign["id"]).values(**values))
        watch.update(values)
        audit = get(conn, "obligation", watch["audit_obligation_id"]) if watch["audit_obligation_id"] else None
        owner = get(conn, "assignment", audit["assignment_id"]) if audit else None
        if audit and audit["status"] == "open" and owner["status"] in ACTIVE:
            continue
        follow_up = follow_up_status(conn, campaign["id"], audit, now,
                                     timeout_seconds=scheduler.config.coordination_stall_seconds)
        # Fresh productive evidence satisfies the old wait. Failed/misprofiled
        # owners still require repair even when their failure changes the hash.
        attention = follow_up["status"] == "attention_required" and (
            watch["audit_frontier_hash"] == observed or any(
                row["blocker"] and row["status"] != "completed" for row in follow_up["owners"]))
        if audit and watch["audit_frontier_hash"] == observed and audit["status"] != "open":
            if not campaign["status_note"] or campaign["status_note"].startswith(NOTE_PREFIX):
                note = NOTE_PREFIX + ("; ".join(follow_up["issues"]) if attention else
                    f"recovery decision recorded; follow-up evidence due by {follow_up['deadline'].isoformat()}.")
                if campaign["status_note"] != note:
                    change(conn, "run", campaign["id"], status_note=note)
            if not attention:
                continue
        passes = conn.execute(select(func.count()).select_from(a).where(a.c.run_id == campaign["id"],
            a.c.automation_id.is_not(None), a.c.status == "completed",
            a.c.finished_at >= watch["last_progress_at"])).scalar_one()
        if not attention and not (audit and audit["status"] == "open") and not recovery_due(watch, now, passes, scheduler.config):
            continue
        rule = conn.execute(select(automation).where(automation.c.run_id == campaign["id"],
            automation.c.enabled.is_(True), automation.c.functions.contains(["planner"]),
            automation.c.role == "worker").order_by(automation.c.created_at).limit(1)).mappings().first()
        if not rule or (rule["not_before"] and rule["not_before"] > now):
            continue
        candidates = list(conn.execute(select(a).where(a.c.automation_id == rule["id"],
            a.c.status.in_(ACTIVE)).order_by(a.c.queue_rank, a.c.id)).mappings())
        current_owner = next((row for row in candidates if audit and row["id"] == audit["assignment_id"]), None)
        if any(row["status"] != "pending" for row in candidates) and not (attention and current_owner):
            continue
        candidate = dict(current_owner or candidates[0]) if candidates else scheduler.replenish(conn, actor, dict(rule))
        if not candidate:
            continue
        # Preserve operator timing, budgets, backoff and physical admission.
        timed = evaluate(candidate["start_condition"], now, lambda _: Evaluation(Truth.UNKNOWN, "External event"))
        if timed.truth is Truth.FALSE:
            continue
        probe = {**candidate, "start_condition": None}
        running_owner = attention and candidate["status"] == "running"
        if not running_owner and (not scheduler.service.readiness(conn, probe, now).ready or scheduler.admission_blocker(conn, probe)):
            continue
        description = ("Coordination recovery: the run has completed " + str(passes) +
            " recurring passes without changed artifact, review or worker-outcome evidence. "
            "Read coordination.memory: previous handoffs, failed approaches, current PR findings, "
            "unresolved obligations and owners. Inspect the relevant Zulip/Forge replies. "
            "Explain what the previous plan expected, why it did not happen, and choose a concrete "
            "repair, refactoring or justified external wait. Reorder existing work before duplicating it. "
            "Record the next owner and independently producible unlock condition in this obligation's "
            "resolution. Completed handling must link evidence of the action or event-conditioned automation. "
            "Finishing a session or posting another status update is not evidence of progress.")
        if attention:
            description += " Previous follow-up requires attention: " + "; ".join(follow_up["issues"])
            description += f". Inspect the previous recovery decision at /api/v3/records/obligation/{audit['id']}."
        # Preserve the old decision and its exact promise. One new open
        # obligation, on the existing eligible planner when possible, owns the
        # failed follow-through; subsequent ticks reuse it until it is resolved.
        new = create(conn, "obligation", assignment_id=candidate["id"],
            number=next_number(conn, "obligation", "assignment_id", candidate["id"]),
            kind="decision", description=description)
        if audit and audit["status"] == "open":
            change(conn, "obligation", audit["id"], status="handled", resolution={"kind": "scheduled",
                "assignment_id": str(candidate["id"]), "note": "Recovery owner ended; this planner inherits the unresolved decision."})
        conn.execute(update(watch_table).where(watch_table.c.run_id == campaign["id"]).values(
            audit_obligation_id=new["id"], audit_frontier_hash=observed))
        note = NOTE_PREFIX + f"recovery assigned to A{candidate['number']}; {passes} passes without new evidence."
        rank = conn.execute(select(func.min(a.c.queue_rank)).where(a.c.run_id == campaign["id"],
            a.c.status == "pending")).scalar_one()
        updated = change(conn, "assignment", candidate["id"], start_condition=None,
                         queue_rank=(rank or 0) - 1024, status_note=note)
        if not campaign["status_note"] or campaign["status_note"].startswith(NOTE_PREFIX):
            changed_run = change(conn, "run", campaign["id"], status_note=note)
            emit(conn, actor.id, project_of(conn, "run", campaign["id"]), "run", changed_run,
                 ["status_note"], note=note)
        emit(conn, actor.id, project_of(conn, "run", campaign["id"]), "assignment", updated,
             ["start_condition", "queue_rank", "status_note"], note=note)
        emit(conn, actor.id, project_of(conn, "run", campaign["id"]), "obligation", new,
             ["status", "resolution", "description"], note=note)
        changed += 1
    return changed
