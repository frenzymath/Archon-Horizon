from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from archon_horizon.pipeline.coordination_memory import (
    awaiting_evidence, episode_status, fingerprint, follow_up_status, frontier, memory, reconcile,
    recovery_due, state,
)
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.records import change, create, get
from archon_horizon.pipeline.schema import tables
from test_pipeline_service import service_database, world


def test_coordination_queries_compile_for_postgres():
    from sqlalchemy.dialects import postgresql
    from archon_horizon.pipeline.review_backlog import current_objection

    class EmptyResult:
        def mappings(self):
            return self

        def scalars(self):
            return self

        def first(self):
            return None

        def __iter__(self):
            return iter(())

    class CompileConnection:
        def execute(self, query):
            query.compile(dialect=postgresql.dialect())
            return EmptyResult()

    conn = CompileConnection()
    rid = uuid4()
    assert frontier(conn, rid) == fingerprint([], [], [], [])
    assert memory(conn, rid)["recent_handoffs"] == []
    item = tables["forge_item"]
    conn.execute(select(item).where(~current_objection(item)))


def test_coordination_migration_emits_non_destructive_postgres_ddl():
    from io import StringIO
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from archon_horizon.pipeline.migrations.versions.v0023_run_coordination import upgrade

    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    with Operations.context(context):
        upgrade()
    sql = output.getvalue()
    assert "CREATE TABLE run_coordination" in sql
    assert "REFERENCES obligation (id) ON DELETE RESTRICT" in sql
    assert "DROP" not in sql and "UPDATE" not in sql


def test_duplicate_evidence_and_order_do_not_restart_an_episode():
    commit = {"repository_id": "repo", "commit_oid": "abc"}
    review = {"reviewer": "definitions", "verdict": "changes_requested", "summary": "Missing producer"}
    original = fingerprint([commit], [review], [], [])
    assert fingerprint([commit, commit], [review, review], [], []) == original
    assert fingerprint([{**commit, "commit_oid": "def"}], [review], [], []) != original
    assert fingerprint([commit], [{**review, "verdict": "approved"}], [], []) != original
    assert fingerprint([commit], [review], [], [{"status": "failed", "task": "Repair producer"}]) != original


@pytest.mark.parametrize("seconds,passes,due", [(60, 2, False), (60, 3, True), (1800, 0, True)])
def test_recovery_uses_evidence_age_or_repeated_passes(seconds, passes, due):
    now = datetime.now(timezone.utc)
    watch = {"last_progress_at": now - timedelta(seconds=seconds)}
    config = SimpleNamespace(coordination_repeat_passes=3, coordination_stall_seconds=1800)
    assert recovery_due(watch, now, passes, config) is due


def test_episode_distinguishes_an_owned_review_from_a_lost_owner():
    watch = {"frontier_hash": "same", "audit_frontier_hash": "same"}
    assert episode_status(watch, None, None) == "observing"
    assert episode_status(watch, {"status": "open"}, {"status": "pending"}) == "review_needed"
    assert episode_status(watch, {"status": "open"}, {"status": "failed"}) == "owner_lost"
    assert episode_status(watch, {"status": "handled"}, {"status": "completed"}) == "awaiting_result"
    assert episode_status({**watch, "frontier_hash": "new"}, {"status": "handled"},
                          {"status": "completed"}) == "observing"


def test_recovery_persists_one_decision_and_reopens_on_changed_evidence(world):
    run = world.run()
    now = world.conn.execute(select(func.now())).scalar_one()
    assert reconcile(world.scheduler, world.conn, world.actor, now - timedelta(minutes=31)) == 0
    assert reconcile(world.scheduler, world.conn, world.actor, now) == 1
    watch = state(world.conn, run["id"])
    audit = get(world.conn, "obligation", watch["audit_obligation_id"])
    owner = get(world.conn, "assignment", audit["assignment_id"])
    assert owner["start_condition"] is None
    assert audit["kind"] == "decision"
    assert reconcile(world.scheduler, world.conn, world.actor, now + timedelta(minutes=1)) == 0
    change(world.conn, "obligation", audit["id"], status="done",
           resolution={"kind": "completed", "note": "Reordered the existing repair; inspect its result", "evidence": []})
    change(world.conn, "assignment", owner["id"], status="completed", finished_at=now)
    from archon_horizon.pipeline.scheduler import Scheduler
    restarted = Scheduler(world.service)
    assert reconcile(restarted, world.conn, world.actor, now + timedelta(minutes=10)) == 0
    assert awaiting_evidence(world.conn, run["id"])
    repair = world.assignment(run)
    change(world.conn, "assignment", repair["id"], status="failed", finished_at=now)
    assert reconcile(restarted, world.conn, world.actor, now + timedelta(minutes=20)) == 0
    assert not awaiting_evidence(world.conn, run["id"])
    assert reconcile(restarted, world.conn, world.actor, now + timedelta(minutes=51)) == 1


def recovery_with_followup(world):
    run = world.run()
    now = world.conn.execute(select(func.now())).scalar_one()
    reconcile(world.scheduler, world.conn, world.actor, now - timedelta(minutes=31))
    decided = now
    reconcile(world.scheduler, world.conn, world.actor, decided)
    audit = get(world.conn, "obligation", state(world.conn, run["id"])["audit_obligation_id"])
    repair = world.assignment(run)
    change(world.conn, "obligation", audit["id"], status="handled",
           resolution={"kind": "scheduled", "assignment_id": str(repair["id"]), "note": "Repair the missing contract"})
    return run, get(world.conn, "obligation", audit["id"]), repair, decided


def test_overdue_recovery_reuses_planner_and_preserves_prior_promise(world):
    run, audit, repair, decided = recovery_with_followup(world)
    before = decided + timedelta(minutes=29)
    assert reconcile(world.scheduler, world.conn, world.actor, before) == 0
    assert awaiting_evidence(world.conn, run["id"], now=before)
    overdue = decided + timedelta(minutes=31)
    assert follow_up_status(world.conn, run["id"], audit, overdue)["status"] == "attention_required"
    assert not awaiting_evidence(world.conn, run["id"], now=overdue)
    assert reconcile(world.scheduler, world.conn, world.actor, overdue) == 1
    new = get(world.conn, "obligation", state(world.conn, run["id"])["audit_obligation_id"])
    assert new["assignment_id"] == audit["assignment_id"]
    assert new["status"] == "open"
    assert "deadline" in new["description"]
    assert get(world.conn, "obligation", audit["id"])["resolution"]["assignment_id"] == str(repair["id"])
    assert reconcile(world.scheduler, world.conn, world.actor, overdue + timedelta(hours=1)) == 0
    decisions = world.conn.execute(select(func.count()).select_from(tables["obligation"]).where(
        tables["obligation"].c.assignment_id == audit["assignment_id"],
        tables["obligation"].c.number > 1,
        tables["obligation"].c.kind == "decision", tables["obligation"].c.status == "open")).scalar_one()
    assert decisions == 1


@pytest.mark.parametrize("fault", ["failed", "cancelled", "completed", "supervisor", "budget"])
def test_invalid_recovery_followup_is_visible_before_deadline(world, fault):
    run, audit, repair, decided = recovery_with_followup(world)
    if fault in ("failed", "cancelled", "completed"):
        change(world.conn, "assignment", repair["id"], status=fault, finished_at=decided)
    elif fault == "supervisor":
        change(world.conn, "assignment", repair["id"], role="maintainer", functions=["orchestrator"])
    else:
        run = change(world.conn, "run", run["id"], max_assignments=1)
        admitted = world.assignment(run)
        change(world.conn, "assignment", admitted["id"], status="completed", started_at=decided, finished_at=decided)
    observed = follow_up_status(world.conn, run["id"], audit, decided + timedelta(minutes=1))
    assert observed["status"] == "attention_required"
    assert observed["owners"][0]["blocker"]
    if fault not in ("budget", "completed"):
        assert reconcile(world.scheduler, world.conn, world.actor, decided + timedelta(minutes=1)) == 1
        assert reconcile(world.scheduler, world.conn, world.actor, decided + timedelta(minutes=2)) == 0


def test_changed_productive_evidence_clears_overdue_followup(world):
    run, audit, repair, decided = recovery_with_followup(world)
    create(world.conn, "forge_item", repository_id=world.workspace_repo["id"], origin_run_id=run["id"],
           remote_number=1, kind="pull_request", title="Repaired contract", status="open",
           head_commit_oid="b" * 40, observed_at=decided)
    observed = decided + timedelta(minutes=31)
    assert reconcile(world.scheduler, world.conn, world.actor, observed) == 0
    watch = state(world.conn, run["id"])
    assert watch["last_progress_at"] == observed
    assert watch["audit_obligation_id"] == audit["id"]
    assert not awaiting_evidence(world.conn, run["id"], now=observed)


@pytest.mark.parametrize("delivery", ["pending", "failed", "completed"])
def test_recovery_frontier_tracks_inherited_pr_only_after_delivered_amendment(world, delivery):
    old_run = world.run()
    world.disable_automations(old_run)
    change(world.conn, "run", old_run["id"], status="cancelled")
    run = world.run()
    now = world.conn.execute(select(func.now())).scalar_one()
    item = create(world.conn, "forge_item", repository_id=world.workspace_repo["id"],
        origin_run_id=old_run["id"], remote_number=1, kind="pull_request", title="Inherited contract",
        status="open", head_commit_oid="a" * 40, observed_at=now)
    unrelated = create(world.conn, "forge_item", repository_id=world.workspace_repo["id"],
        origin_run_id=old_run["id"], remote_number=2, kind="pull_request", title="Other work",
        status="open", head_commit_oid="a" * 40, observed_at=now)
    create(world.conn, "outbox_operation", project_id=world.project["id"], actor_principal_id=world.actor.id,
        kind="forge_change", schema_version=1, idempotency_key="recovery-amendment", status=delivery,
        payload={"origin_run_id": str(run["id"]), "forge_item_id": str(item["id"])})
    initial = frontier(world.conn, run["id"])
    change(world.conn, "forge_item", unrelated["id"], head_commit_oid="c" * 40)
    assert frontier(world.conn, run["id"]) == initial
    change(world.conn, "forge_item", item["id"], head_commit_oid="b" * 40)
    amended = frontier(world.conn, run["id"])
    assert (amended != initial) is (delivery == "completed")
    assessment = create(world.conn, "forge_review", forge_item_id=item["id"], remote_id="review-1",
        reviewer_remote_id="definitions", verdict="approved", summary="Repaired definition checked",
        commit_oid="b" * 40, observed_at=now)
    assert (frontier(world.conn, run["id"]) != amended) is (delivery == "completed")
    assert [row["id"] for row in memory(world.conn, run["id"])["current_head_reviews"]] == (
        [assessment["id"]] if delivery == "completed" else [])
    assert get(world.conn, "forge_item", item["id"])["origin_run_id"] == old_run["id"]


def test_disabled_event_conditioned_recovery_automation_is_visible(world):
    run, audit, repair, decided = recovery_with_followup(world)
    rule = world.conn.execute(select(tables["automation"]).where(
        tables["automation"].c.run_id == run["id"], tables["automation"].c.name == "maintainer")).mappings().one()
    change(world.conn, "automation", rule["id"], enabled=False)
    audit = change(world.conn, "obligation", audit["id"], status="done", resolution={"kind": "completed",
        "note": "Wait for maintainer", "evidence": [{"kind": "automation", "id": str(rule["id"])}]})
    observed = follow_up_status(world.conn, run["id"], audit, decided)
    assert observed["status"] == "attention_required"
    assert any("disabled" in issue for issue in observed["issues"])


def test_recovery_uses_delegated_successor_not_completed_evidence_author(world):
    run, audit, maintainer, decided = recovery_with_followup(world)
    change(world.conn, "assignment", maintainer["id"], role="maintainer", status="running")
    historical = world.assignment(run, role="maintainer")
    handoff = world.ledger(historical["id"])[0]
    change(world.conn, "obligation", handoff["id"], status="handled", resolution={"kind": "delegated",
        "assignment_ids": [str(maintainer["id"])], "note": "The next maintainer owns the gate repair"})
    change(world.conn, "assignment", historical["id"], status="completed", finished_at=decided)
    audit = change(world.conn, "obligation", audit["id"], status="done", resolution={"kind": "completed",
        "note": "The live maintainer owns the gate repair; the historical blocker records why", "evidence": [
            {"kind": "assignment", "id": str(maintainer["id"])},
            {"kind": "obligation", "id": str(handoff["id"])}]})
    observed = frontier(world.conn, run["id"])
    world.conn.execute(update(tables["run_coordination"]).where(
        tables["run_coordination"].c.run_id == run["id"]).values(
            frontier_hash=observed, audit_frontier_hash=observed))
    status = follow_up_status(world.conn, run["id"], audit, decided + timedelta(minutes=1))
    assert status["status"] == "awaiting_result"
    assert [owner["id"] for owner in status["owners"]] == [maintainer["id"]]
    for minute in (1, 2, 3):
        assert reconcile(world.scheduler, world.conn, world.actor, decided + timedelta(minutes=minute)) == 0
        assert state(world.conn, run["id"])["audit_obligation_id"] == audit["id"]


def test_explicit_recovery_owner_takes_precedence_over_historical_assignment_evidence(world):
    run, audit, repair, decided = recovery_with_followup(world)
    old = world.assignment(run)
    change(world.conn, "assignment", old["id"], status="failed", finished_at=decided)
    audit = {**audit, "resolution": {**audit["resolution"], "evidence": [
        {"kind": "assignment", "id": str(old["id"])}]}}
    observed = follow_up_status(world.conn, run["id"], audit, decided)
    assert observed["status"] == "awaiting_result"
    assert [owner["id"] for owner in observed["owners"]] == [repair["id"]]


@pytest.mark.parametrize("kind", ["assignment", "obligation"])
def test_completed_evidence_is_not_a_promised_live_owner(world, kind):
    run, audit, repair, decided = recovery_with_followup(world)
    root = world.ledger(repair["id"])[0]
    change(world.conn, "obligation", root["id"], status="done", resolution={"kind": "completed",
        "note": "Historical work is complete", "evidence": []})
    change(world.conn, "assignment", repair["id"], status="completed", finished_at=decided)
    audit = change(world.conn, "obligation", audit["id"], status="done", resolution={"kind": "completed",
        "note": "Historical work evidence", "evidence": [
            {"kind": kind, "id": str(repair["id"] if kind == "assignment" else root["id"])}]})
    observed = follow_up_status(world.conn, run["id"], audit, decided)
    assert observed["status"] == "awaiting_result"
    assert observed["owners"] == []
    assert follow_up_status(world.conn, run["id"], audit, decided + timedelta(minutes=31))["status"] == "attention_required"


@pytest.mark.parametrize("broken", [False, True])
def test_recovery_follows_superseded_then_delegated_obligations(world, broken):
    run, audit, repair, decided = recovery_with_followup(world)
    historical = world.assignment(run)
    original = world.ledger(historical["id"])[0]
    replacement = create(world.conn, "obligation", assignment_id=historical["id"], number=2,
        description="Transferred repair", status="handled", resolution={"kind": "scheduled",
            "assignment_id": str(repair["id"]), "note": "Repair owner is queued"})
    change(world.conn, "obligation", original["id"], status="superseded", resolution={"kind": "superseded",
        "replacement_obligation_ids": [str(replacement["id"])], "note": "Use the updated contract"})
    change(world.conn, "assignment", historical["id"], status="completed", finished_at=decided)
    if broken:
        change(world.conn, "assignment", repair["id"], status="failed", finished_at=decided)
    audit = change(world.conn, "obligation", audit["id"], status="superseded", resolution={"kind": "superseded",
        "replacement_obligation_ids": [str(original["id"])], "note": "The repair is owned there"})
    observed = follow_up_status(world.conn, run["id"], audit, decided)
    assert observed["status"] == ("attention_required" if broken else "awaiting_result")
    assert [owner["id"] for owner in observed["owners"]] == [repair["id"]]


def test_recovery_obligation_cycle_is_reported_without_recursive_scanning(world):
    run, audit, repair, decided = recovery_with_followup(world)
    first = world.ledger(repair["id"])[0]
    second = create(world.conn, "obligation", assignment_id=repair["id"], number=2, description="Cyclic follow-up")
    for old, replacement in ((first, second), (second, first)):
        change(world.conn, "obligation", old["id"], status="superseded", resolution={"kind": "superseded",
            "replacement_obligation_ids": [str(replacement["id"])], "note": "Bad historical replacement"})
    audit = change(world.conn, "obligation", audit["id"], status="superseded", resolution={"kind": "superseded",
        "replacement_obligation_ids": [str(first["id"])], "note": "Inspect the historical replacement"})
    observed = follow_up_status(world.conn, run["id"], audit, decided)
    assert observed["status"] == "attention_required"
    assert any("cycle" in issue for issue in observed["issues"])


@pytest.mark.parametrize("guard", ["paused", "budget", "disabled", "delay", "retry"])
def test_recovery_preserves_operator_and_admission_guards(world, guard):
    run = world.run()
    now = world.conn.execute(select(func.now())).scalar_one()
    reconcile(world.scheduler, world.conn, world.actor, now)
    a = tables["assignment"]
    owner = world.conn.execute(select(a).where(a.c.run_id == run["id"],
        a.c.functions.contains(["planner"]))).mappings().one()
    if guard == "paused":
        change(world.conn, "run", run["id"], status="paused")
    elif guard == "budget":
        change(world.conn, "run", run["id"], token_budget=0)
    elif guard == "disabled":
        change(world.conn, "automation", owner["automation_id"], enabled=False)
    else:
        change(world.conn, "assignment", owner["id"],
               **{"not_before" if guard == "delay" else "retry_at": now + timedelta(days=2)})
    assert reconcile(world.scheduler, world.conn, world.actor, now + timedelta(days=1)) == 0
    assert state(world.conn, run["id"])["audit_obligation_id"] is None


def test_recovery_reassigns_a_lost_diagnostic_owner(world):
    run = world.run()
    now = world.conn.execute(select(func.now())).scalar_one()
    reconcile(world.scheduler, world.conn, world.actor, now)
    reconcile(world.scheduler, world.conn, world.actor, now + timedelta(hours=1))
    old = get(world.conn, "obligation", state(world.conn, run["id"])["audit_obligation_id"])
    change(world.conn, "assignment", old["assignment_id"], status="failed", finished_at=now)
    assert reconcile(world.scheduler, world.conn, world.actor, now + timedelta(hours=2)) == 1
    new = get(world.conn, "obligation", state(world.conn, run["id"])["audit_obligation_id"])
    assert new["assignment_id"] != old["assignment_id"]
    assert get(world.conn, "obligation", old["id"])["resolution"]["assignment_id"] == str(new["assignment_id"])


def test_recovery_cannot_close_with_an_unsupported_status_note(world):
    from archon_horizon.pipeline.models import ObligationResolve

    run = world.run()
    now = world.conn.execute(select(func.now())).scalar_one()
    reconcile(world.scheduler, world.conn, world.actor, now)
    reconcile(world.scheduler, world.conn, world.actor, now + timedelta(hours=1))
    audit = get(world.conn, "obligation", state(world.conn, run["id"])["audit_obligation_id"])
    with pytest.raises(DomainError, match="Link the repaired work"):
        world.service.resolve_obligation(world.conn, world.actor, audit["id"], ObligationResolve(
            expected_revision=audit["revision"], status="done",
            resolution={"kind": "completed", "note": "Still waiting", "evidence": []}))
    assert get(world.conn, "obligation", audit["id"])["status"] == "open"


def test_memory_exposes_failed_followup_and_preserves_original_findings(world):
    run = world.run()
    author = world.assignment(run)
    repair = world.assignment(run)
    blocker = create(world.conn, "obligation", assignment_id=author["id"], number=2, kind="blocker",
                     description="The endpoint consumes an unconstructed quotient", status="handled",
                     resolution={"kind": "scheduled", "assignment_id": str(repair["id"]), "note": "Construct it"})
    change(world.conn, "assignment", repair["id"], status="failed", finished_at=func.now())
    context = memory(world.conn, run["id"])
    entry = next(row for row in context["unresolved"] if row["id"] == blocker["id"])
    assert entry["follow_up_owners"][0]["status"] == "failed"
    assert "unconstructed quotient" in entry["description"]
    assert context["recent_handoffs"][0]["id"] == repair["id"]


def test_identical_commit_publications_do_not_count_as_new_evidence(world):
    run = world.run()
    worker = world.assignment(run)
    commit = create(world.conn, "artifact", project_id=world.project["id"], kind="commit",
                    content={"repository_id": str(world.workspace_repo["id"]), "commit_oid": "a" * 40})
    def publish(ref_name):
        create(world.conn, "publication", artifact_id=commit["id"], requested_by_assignment_id=worker["id"],
               target={"ref_name": ref_name}, status="verified", verified_at=func.now())
    publish("refs/heads/repair")
    original = frontier(world.conn, run["id"])
    publish("refs/heads/recovery")
    assert frontier(world.conn, run["id"]) == original


def test_pending_owner_cannot_wait_for_its_own_open_obligation(world):
    run = world.run()
    owner = world.assignment(run)
    obligation = world.ledger(owner["id"])[0]
    with pytest.raises(DomainError, match="cycle"):
        world.command("update_assignment", owner, start_condition={"version": 1, "expression": {
            "op": "obligation_accounted", "obligation_id": str(obligation["id"])}})


def test_maintainer_blockers_are_handed_off_instead_of_self_gated(world):
    run = world.run()
    a = tables["assignment"]
    planner = world.conn.execute(select(a).where(a.c.run_id == run["id"],
        a.c.functions.contains(["planner"]))).mappings().one()
    change(world.conn, "assignment", planner["id"], not_before=func.now() + timedelta(hours=1))
    owner = world.assignment(run, role="maintainer")
    blocker = create(world.conn, "obligation", assignment_id=owner["id"], number=2, kind="blocker",
                     description="Resolve the two incompatible representations")
    grant = world.claim()
    assert grant["assignment_id"] == str(owner["id"])
    result = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", grant["execution_id"]), "succeeded")
    assert result["status"] == "completed"
    assert result["start_condition"] is None
    replacement = get(world.conn, "obligation", blocker["id"])["resolution"]["replacement_obligation_ids"][0]
    assert get(world.conn, "obligation", replacement)["assignment_id"] == planner["id"]
    assert any(str(owner["id"]) in item["description"] for item in world.ledger(planner["id"]))
