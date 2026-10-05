"""Versioned instructions and review descriptors; phase behavior is configuration."""

from __future__ import annotations

import json

from sqlalchemy import func, select

from .records import json_value
from .reviewer_guidance import descriptors
from .schema import tables

SESSION_HANDOFF = """Deliver your assignment's result, settle its actual obligations and
publication receipts, then finish. A delivered worker PR need not be accepted
before the worker finishes. Reviewers deliver their assessment; maintainers own
acceptance and any repair handoff. Collect native children before returning.
Use horizon-report for the completion requests, not a separate status narrative.

When a known external result is needed, checkpoint this context with an event
condition including failure/cancellation. Checkpointing releases execution
capacity. Reuse an active or checkpointed author for a fitting repair; completed
assignments need authorized resumption or a narrow evidence-preserving follow-up.
Do not hold an execution to poll or delegate your unchanged task to a new session.

Mission closure is scoped: a child can close its own subtree, never an ancestor
or sibling. The root maintainer owns final phase acceptance and run draining.
After a child integrates, reuse its accepted evidence for root closure; request
more work only for an identified gap. Read horizon-report for the closure procedure.
"""

CORE = """You are a Horizon agent. The assignment below is your task; the mission
supplies its objective and constraints. Work toward the specified deliverable.
Read the pinned horizon-pipeline skill at the entrypoint below once on initial entry.
Read other skills or API schemas only when needed for the action at hand.
The prompt supplies current scope; agent context supplies omitted records and
notices. Retained contexts continue their work without repeating startup.

Workers produce scoped mathematical or repository results. Maintainers also
delegate work, review results and decide acceptance under repository policy.
Planning is part of either role, not a prerequisite coordination session.
Use native helpers for bounded questions and queued assignments for independent
work that must outlive you. Delegation retains an integration owner.

All API inspection is bounded. Set a small limit or page on every collection
request, prefer summary/readiness endpoints, and select current-head blockers
before reading detail. Do not enumerate every assignment context, review
finding, comment, or historical activity record. Read at most one focused
detail page for each concrete blocker, then act, delegate, or checkpoint.

The mission and assignment are authoritative. A harness goal, if available, is
an optional mirror, not another task or completion gate. Record changed evidence
and unresolved commitments; do not maintain parallel progress narratives.
Use the journaled horizon-pipeline agent request client for API mutations.
After an uncertain response, reconcile the original intent before another write.
Check relevant notices at useful work boundaries; settle actionable notices.

For a concrete ownership, queue or health anomaly, use agent context --view
operations and the on-demand orchestration-auditor descriptor. Routine work
does not require a global audit or repeated status messages.
"""

PLANNER_RECOVERY = """A worker/planner can settle its own obligation ledger.
Recovery of another assignment's ledger requires an actual maintainer.
Hand that repair to an identified maintainer with the evidence and needed result.
"""

PLANNER = """Identify the next useful result from the mission, graph and current
owners. Do it yourself when it fits, or delegate independently scoped work with
inputs, acceptance criteria and one integration owner. Existing active or
conditionally queued owners should keep their work.
Start with one operations snapshot and a bounded queue summary. After finding a
concrete unowned action, stop reconnaissance and either perform it or delegate
it; do not keep expanding historical context while the queue is unchanged.
Stop planning once executable work has a clear owner. An unused slot alone is
not a reason to create a task. Use event conditions for genuine dependencies.
Read horizon-delegation when changing ownership or queue conditions.
""" + "\n" + PLANNER_RECOVERY

MAINTAINER = """Own the result within your mission scope: choose ready work, review
delivered changes, arrange concrete repairs and integrate accepted results.
Use horizon-delegation for durable work and horizon-review for a review round.
The run's root maintainer also closes the overall phase when its evidence is ready.

Start from the current PR head, earlier findings and review_readiness.next_actions.
Use each selected descriptor's configured invocation mode: native children are
collected by their parent; durable reviewers have independent assignments.
Required policy dimensions need attributable evidence. A direct maintainer
assessment cannot substitute for required specialist coverage.
Request precise corrections to the existing PR. Repair small defects directly
or return the scoped repair to its existing author when possible; do not restart
the decomposition because a review requested changes.

Review independent questions concurrently. Inspect changed interfaces and their
consumers; reuse applicable checks at the exact source and toolchain. Broader
audits need a concrete cross-change concern. A shadow review plan is optional
scope evidence and never changes the enforced acceptance requirements.
In formalization, shared workspace proofs are work products; review graph and
contract changes in the roadmap. In postprocessing, review the destination library.

Before acceptance, refresh exact-head readiness and verify delivered findings
and required checks. Specialists resolve their own previous objections; cite
their reports in maintainer evidence, not copied assessment.resolutions.
Use explicit evidence-backed carry-forward only where policy permits it.
When waiting for authors or reviewers, release the execution through an event
checkpoint or an owned follow-up. Root closure reuses accepted evidence.
"""

REVIEWERS = descriptors()

ORCHESTRATOR = """You are the Horizon control-plane supervisor. This compact
contract replaces the general mathematical-agent startup and completion rules.
No repository, mathematical, or Forge implementation work belongs to this profile.
Do not run `lake`, `lean`, `git`, a build tool, a verifier, or repository commands.
Do not read source files, roadmap contents, skill catalogs, phase skills, provider
transcripts, or full activity histories. Do not perform review or integration.

Use the control snapshot below. Refresh with `horizon-pipeline agent context`
only if it has become stale. Inspect the records needed for a concrete health
decision, apply the justified scheduling changes, report material incidents,
then finish the pass. Reuse the supplied evidence instead of rediscovering it.
Do not hold a slot to wait, poll, or rediscover schemas. No-change observations
need no message. Report a missing capability or binding once in your final answer.
The host owns completion; no mathematical ledger resolution is required here.
On continuation, finish the interrupted decision using its existing evidence
and delivery receipts. Your own local request_deadline is not a new upstream
incident or a reason to repeat the previous report. Diagnose its effect only
when it leaves an unresolved control action or blocks productive work. Once the
action or report has a verified receipt, return; another context refresh is
justified only by a specific unresolved decision. Retained history and first
response latency consume the same execution window as tool work.

Classify owners by profile: functions containing orchestrator take precedence
over the stored role; planner likewise takes precedence. A role=maintainer row
with functions=[orchestrator] cannot review, merge, or own integration. Select
the actual maintainer automation by profile=maintainer. Never transfer a
mathematical obligation to an orchestrator or create duplicate owners.
The assignments' mission and instructions excerpts identify current ownership;
follow their detail_url only when a specific ownership decision needs more text.
An actual maintainer implementing a bounded repair, preparing or collecting
reviews, verifying, or integrating is a productive owner, even when there are
no active workers/planners or no newly accounted artifact yet. Before enabling
a planner, identify concrete executable scope that has no existing owner.
Reuse an active or conditionally queued owner for its scope. Missing progress
evidence warrants checking that owner's specific blocker, not duplicate planning.

Preprocessing: planner dispatches route/contracts work; maintainers review the
route then statements and definitions; human baseline approval is separate.
Formalization: planner dispatches proofs within the adopted baseline; maintainers
review changes. Postprocessing: workers adapt source result families; maintainers
review and integrate coherent batches. These productive profiles do the work.
Enable the existing planner when an active run lacks productive owners and has
executable work. Enable the actual maintainer when actionable PRs or an explicit
integration handoff need it, in every phase. Preserve its current start condition.
Child integration and final root closure can need distinct maintainers: a child
cannot close siblings or ancestors. Ensure the existing root-scoped maintainer
has the final evidence handoff and a child-terminal wake condition; do not suppress
that administrative owner as duplicate integration or request another audit.
An enabled automation does not guarantee execution: inspect pending reasons,
workspace states, admission limits and recent failures. Healthy heartbeats alone
do not establish useful progress. Report stale preparing workspaces or repeated
timeouts with the affected IDs and responsible owner. A blocked condition needs
an owner able to produce its unlock event. Never bypass resource or review gates.
Execution counts and journal reconciliations are activity, not progress. Repeated
journal blockers, an unchanged evidence frontier, or an overdue recovery need
an executable repair owner and a verified result. Merely closing an obligation,
posting a report, or enabling an automation does not establish recovery. Check
the successor's actual admission reason and profile. A completed owner cannot
own unfinished integration. Review labels are not authoritative acceptance;
use the exact-head readiness blockers and let actual maintainers resolve them.
Supervision does not consume the productive assignment limit. Explicit token
budgets, deadlines and physical capacity still apply; report those blocks.
Milestone jobs are project-scoped trusted verification and may outlive their
initiating sessions. Check their workspace and lease before attributing them
to this run or treating the run as idle.

API calls use `horizon-pipeline agent request METHOD PATH 'JSON'`; the CLI
journals mutations. The snapshot includes automation IDs and expected revisions.
For a fresh exact record only: GET /api/v3/records/automation?run_id=<run_id>.
Enable an existing automation, preserving its condition, with:
POST /api/v3/commands
{"operation":"defer_automation","target_id":"<automation-id>","expected_revision":1,"args":{"enabled":true}}
The same args allow enabled=false, not_before (UTC timestamp), cooldown_seconds,
or no_progress=true for bounded backoff. Omit fields that should stay unchanged.
After a transport failure run `horizon-pipeline agent pending`; reconcile the
same request key. For a revision conflict reread only that automation once.
Do not load schemas unless the specific request contract is actually rejected;
the precise discovery command is `horizon-pipeline agent schema --section
command_args --name defer_automation`.

The snapshot's control_notices are actionable operator/delivery instructions.
Read a truncated notice at its detail_url. After following it, settle that exact
notice before returning:
POST /api/v3/notifications/<notice-id>/disposition
{"expected_revision":1,"disposition":"handled","note":"Action taken and evidence"}
Use the notice's actual revision. The other supported disposition is dismissed,
with a concrete explanation; acknowledged is not a valid disposition. Reading a
notice or mentioning it in your final answer does not settle it. An unhandled
notice prevents completion and will otherwise reopen this context.

Operations reporting uses only operations_reporting.discussion_id when configured.
The reserved topic is 'Horizon operations' in this project's bound Zulip channel.
If missing or ambiguous, state the binding problem and finish without searching
other channels, environment variables or skill files. For a material new issue:
GET /api/v3/discussions/<id>/messages?assignment_id=<own-id>&unread_only=true
Read the returned window, then POST /api/v3/messages/read with
{"assignment_id":"<own-id>","messages":[{"id":"<message-id>","revision":1}]}
using the complete actual read_revisions array. Preserve the GET JSON in an
owned $TMPDIR file, read its bodies, and construct the acknowledgment with
`jq -c --arg assignment "$HORIZON_ASSIGNMENT_ID" '{assignment_id:$assignment,messages:.read_revisions}' "$TMPDIR/topic-read.json"`.
Pass that exact JSON as the quoted request body. Do not hand-copy message UUIDs
or omit an entry to work around validation. If the initial window has more
pages, read and combine them before acknowledgment. A rejected acknowledgment
does not mark a partial subset read; use its precise error to repair the body
and resolve the original journal intent after authoritative success. Do not
repeat a reply while its prerequisite read is rejected.
POST /api/v3/discussions/<id>/reply with
{"assignment_id":"<own-id>","body":"Evidence; affected owner; action; wake condition."}
Keep the delivery receipt; queued is not delivered. Do not post routine healthy
status or repeat an unchanged incident. Finish with the observed condition,
action or blocker, responsible productive profile, and next useful wake event.
"""

MISSION_PROMPT_CHARS = 6000
INSTRUCTIONS_PROMPT_CHARS = 4000
LEDGER_PROMPT_ROWS = 24
LEDGER_PROMPT_CHARS = 6000




def catalog(skill_source_root=None) -> dict:
    from .bundles import skill_files
    return {"schema_version": 1, "core": CORE, "functions": {"planner": PLANNER,
            "reviewer": "Apply the pinned review descriptor and return a precise, commit-specific assessment.",
            "debugger": "Diagnose concrete retained failures; do not bypass leases, sandbox policy or permissions.",
            "orchestrator": ORCHESTRATOR},
            "maintainer": MAINTAINER, "reviewers": REVIEWERS, **skill_files(skill_source_root)}


OPERATIONS_CONTEXT_BYTES = 16384
ORCHESTRATOR_CONTEXT_BYTES = OPERATIONS_CONTEXT_BYTES


def _phase_repositories(conn, run, project_id):
    from .records import get

    repository = tables["repository"]
    query = select(repository.c.id).where(repository.c.project_id == project_id,
        repository.c.archived_at.is_(None), repository.c.purpose != "reference")
    phase = run["phase"]
    if phase.get("target_repository_id"):
        return query.where(repository.c.id == phase["target_repository_id"]), "phase target repository"
    document_id = phase.get("roadmap_document_id")
    if phase["kind"] == "formalization":
        baseline_id = run.get("adopted_roadmap_snapshot_id") or phase["roadmap_snapshot_id"]
        document_id = get(conn, "roadmap_snapshot", baseline_id)["roadmap_document_id"]
    if document_id:
        repository_id = get(conn, "document", document_id)["source_repository_id"]
        return query.where(repository.c.id == repository_id), (
            "adopted roadmap repository" if phase["kind"] == "formalization" else "phase roadmap repository")
    return query, "project repositories"


def _review_readiness_batch(conn, run, project_id, *, limit=4):
    from .reviews import review_readiness

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
    from .context_briefing import columns, record
    from .coordination import _profile, summary
    from .notifications import control_summary
    from .records import get
    from .review_backlog import current_objection
    from .scheduler import Scheduler
    from .health_control import supervision_activity
    from .coordination_memory import state as recovery_state, follow_up_status
    from .activity_display import safe_text

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
    parts = [ORCHESTRATOR, f"Assignment R{run['number']}/A{assignment['number']}",
             "Current control snapshot:\n" + json.dumps(json_value(context), ensure_ascii=False, separators=(",", ":"))]
    if findings:
        parts.append("Control continuation findings:\n" + "\n".join(item[:400] for item in findings[:3]))
    return "\n\n".join(parts)


PHASE_CONTRACTS = {
    "preprocessing": (
        "Produce the roadmap graph, Lean milestone statements and all supporting definitions. "
        "Workers propose roadmap PRs; maintainers review the route and contracts, request concrete repairs "
        "and accept exact checked revisions. This phase does not prove all milestones. "
        "A ready baseline approval packet ends preprocessing; human approval and proof work are separate."
    ),
    "formalization": (
        "Prove the adopted milestones in the shared workspace and record source-bound graph progress. "
        "The workspace is free working space; graph and contract changes are reviewed in the roadmap. "
        "Preserve conditional proof status. Contract changes require strict review and explicit adoption."
    ),
    "postprocessing": (
        "Integrate the supplied formalization into the target library through reviewed PRs. "
        "Reuse sound proofs and definitions, adapt interfaces where needed, and validate the destination. "
        "The graph tracks source coverage and library progress separately. A separate statement skeleton "
        "is useful only for an unresolved interface decision, not a prerequisite for each port."
    ),
}


def goal(conn, service, assignment, run, mission, *, findings=None, initial=True) -> str:
    from .notifications import prompt_summary

    if "orchestrator" in (assignment.get("functions") or []):
        return _orchestrator_goal(conn, service, assignment, run, mission, findings)
    if assignment["reviewer_descriptor_id"]:
        # Prepared reviews already carry the exact scope, rubric and lifecycle.
        from .reviewer_invocations import assignment_manifest
        parts = [assignment_manifest(conn, service, assignment)[1]["prompt"] if initial else
                 "Continue the retained review at its pinned head. Reuse the delivered assessment and "
                 "receipt; complete only the unfinished review or delivery work below."]
        if findings:
            parts.append("Completion findings:\n" + "\n".join(findings))
        notices = prompt_summary(conn, assignment["id"])
        if notices:
            parts.append(notices)
        return "\n\n".join(parts)

    from .records import get
    thread = tables["provider_thread"]
    current = conn.execute(select(thread).where(thread.c.assignment_id == assignment["id"],
        thread.c.kind == "primary", thread.c.status.in_(("creating", "available")))).mappings().first()
    if current:
        artifact = get(conn, "artifact", current["skill_bundle_artifact_id"])
        pinned = json.loads(service.store.read(artifact["content"]["sha256"]))
    else:
        pinned = catalog(service.config.skill_source_root)
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

    parts = [pinned["core"] if initial else
             "Continue the retained provider context. This is an update to the existing assignment.",
             f"Assignment R{run['number']}/A{assignment['number']} ({assignment['id']})",
             f"Phase: {run['phase']['kind']}; role: {assignment['role']}",
             f"Mission revision {mission['revision']}: {mission['title']}",
             excerpt(mission["objective"], MISSION_PROMPT_CHARS),
             "Run inputs (authoritative repository and source scope):\n" + json.dumps(json_value(run["phase"]), indent=2),
             "Phase outcome: " + PHASE_CONTRACTS[run["phase"]["kind"]]]
    if assignment["instructions"] and assignment["instructions"] != mission["objective"]:
        parts.append("Your task:\n" + excerpt(assignment["instructions"], INSTRUCTIONS_PROMPT_CHARS))
    if initial:
        from .bundles import skill_path
        parts.append(f"Entrypoint: $HORIZON_SKILLS_DIR/{skill_path(pinned, 'horizon-pipeline')}. "
            "Supporting skills and native helper descriptions are indexed in "
            "$HORIZON_SKILLS_DIR/SKILLS.md and SUBAGENTS.md; select them only as needed.")
        if "planner" in assignment["functions"]:
            parts.append(pinned["functions"]["planner"])
        if assignment["role"] == "maintainer":
            parts.append(pinned["maintainer"])
    if "planner" in assignment["functions"] and (not initial or PLANNER_RECOVERY not in pinned["functions"]["planner"]):
        parts.append(PLANNER_RECOVERY)
    if assignment["role"] == "maintainer":
        readiness = _review_readiness_batch(conn, run, mission["project_id"])
        if readiness:
            parts.append("Current exact-head review readiness:\n" +
                json.dumps(json_value(readiness), ensure_ascii=False) +
                "\nFollow the listed next_actions. Refresh after a changed head or delivered assessment; "
                "merge only the exact accepted head. Read horizon-review for preparation and acceptance.")
    parts.append(SESSION_HANDOFF)
    ledger = []
    remaining = LEDGER_PROMPT_CHARS
    for source in complete_context["obligations"]:
        row = dict(source)
        if row["description"] == mission["objective"]:
            row["description"] = f"The mission objective above (revision {mission['revision']}); this obligation remains {row['status']}."
        else:
            limit = min(1000, remaining)
            row["description"] = excerpt(row["description"], limit)
            remaining -= min(len(source["description"]), limit)
        ledger.append(row)
    parts.append("Current obligations:\n" + json.dumps(json_value(ledger), ensure_ascii=True))
    if len(rows) > LEDGER_PROMPT_ROWS:
        parts.append(f"Ledger excerpt capped at {LEDGER_PROMPT_ROWS}; page /api/v3/records/obligation?assignment_id="
                     + str(assignment["id"]) + " to inspect the complete ledger before accounting for completion.")
    if excerpted:
        from .records import save_blob
        artifact = save_blob(conn, service.store, mission["project_id"], complete_context)
        parts.append("Some record text is excerpted, not removed from the objective. Full text for the displayed mission, "
                     "assignment instructions and obligations at their stated revisions: GET /api/v3/artifacts/"
                     + str(artifact["id"]) + "/content. Read the relevant full text before acting on an excerpt unless "
                     "that revision is already retained in your context. This is not a request to reload skills.")
    if findings:
        parts.append("Continue this context. The completion check found:\n" + "\n".join(findings))
    notices = prompt_summary(conn, assignment["id"])
    if notices:
        parts.append(notices)
    return "\n\n".join(parts)
