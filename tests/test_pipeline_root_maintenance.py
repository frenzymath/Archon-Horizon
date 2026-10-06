from datetime import timedelta

import pytest
from sqlalchemy import func, select

from archon_horizon.pipeline import models
from archon_horizon.pipeline.coordination_memory import frontier, reconcile, state
from archon_horizon.pipeline.records import change, create, get
from archon_horizon.pipeline.root_maintenance import NAME, rule
from archon_horizon.pipeline.scheduler import Scheduler
from archon_horizon.pipeline.schema import tables
from test_pipeline_service import service_database, world


@pytest.fixture
def root_world(world):
    # Exercise production bootstrap; the shared fixture otherwise represents persisted legacy runs.
    world.scheduler = Scheduler(world.service)
    return world


def assignments(world, run):
    return list(world.conn.execute(select(tables["assignment"]).where(
        tables["assignment"].c.run_id == run["id"]).order_by(tables["assignment"].c.number)).mappings())


def settle_pass(world, claim):
    for obligation in world.ledger(claim["assignment_id"]):
        change(world.conn, "obligation", obligation["id"], status="done",
            resolution={"kind": "completed", "note": "The bounded decision has evidence and follow-up ownership", "evidence": []})
    return world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", claim["execution_id"]), "succeeded")


def clear_cooldown(world, run):
    automation = rule(world.conn, run["id"])
    change(world.conn, "automation", automation["id"], not_before=None)
    for item in assignments(world, run):
        if item["automation_id"] == automation["id"] and item["status"] == "pending":
            change(world.conn, "assignment", item["id"], not_before=None)


def test_default_bootstrap_has_one_root_maintainer_and_no_other_control_loops(root_world):
    world = root_world
    run = world.run()
    automation = rule(world.conn, run["id"])
    assert automation["name"] == NAME and automation["enabled"]
    rows = assignments(world, run)
    assert len(rows) == 1
    assert rows[0]["role"] == "maintainer" and rows[0]["functions"] == []
    assert rows[0]["mission_id"] == run["mission_id"]
    assert world.ledger(rows[0]["id"])[0]["kind"] == "decision"
    now = world.conn.execute(select(func.now())).scalar_one()
    assert reconcile(world.scheduler, world.conn, world.actor, now + timedelta(hours=2)) == 0
    assert state(world.conn, run["id"]) is None
    assert world.scheduler.recheck_idle_runs(world.conn, world.actor, now + timedelta(hours=2)) == 0
    assert world.scheduler.replenish_planners(world.conn, world.actor) == 0
    assert world.scheduler.replenish_orchestrators(world.conn, world.actor, now + timedelta(hours=2)) == 0
    assert world.claim()["assignment_id"] == str(rows[0]["id"])


def test_root_forge_condition_is_scoped_to_run_and_phase(root_world):
    world = root_world
    run = world.run()
    other_run = world.run()
    automation = rule(world.conn, run["id"])
    expression = automation["start_condition"]["expression"]["args"][1]
    assert expression["origin_run_id"] == str(run["id"])
    assert expression["review_phase"] == "preprocessing"

    repository = get(world.conn, "repository", world.document["source_repository_id"])
    create(world.conn, "connector_cursor", integration_id=repository["integration_id"],
        consumer="forge:" + str(repository["id"]), status="current", last_synced_at=func.now())
    now = world.conn.execute(select(func.now())).scalar_one()

    def evaluate():
        assignment = next(row for row in assignments(world, run)
                          if row["automation_id"] == automation["id"])
        return world.service.condition_readiness(world.conn,
            {**assignment, "start_condition": {"version": 1, "expression": expression}}, now)

    # A Forge item from another run must not wake this run's maintainer.
    create(world.conn, "forge_item", repository_id=repository["id"], origin_run_id=other_run["id"],
        review_phase="preprocessing", remote_number=101, kind="pull_request", title="Other run",
        status="open", head_commit_oid="a" * 40, observed_at=now)
    assert not evaluate().ready

    # Matching provenance with a different phase must remain outside this run's queue.
    create(world.conn, "forge_item", repository_id=repository["id"], origin_run_id=run["id"],
        review_phase="formalization", remote_number=102, kind="pull_request", title="Wrong phase",
        status="open", head_commit_oid="b" * 40, observed_at=now)
    assert not evaluate().ready

    create(world.conn, "forge_item", repository_id=repository["id"], origin_run_id=run["id"],
        review_phase="preprocessing", remote_number=103, kind="pull_request", title="Current run",
        status="open", head_commit_oid="c" * 40, observed_at=now)
    assert evaluate().ready


def test_spare_capacity_and_own_bookkeeping_do_not_repeat_a_planning_pass(root_world):
    world = root_world
    run = world.run()
    root = world.claim()
    child = world.assignment(run, parent_id=root["assignment_id"])
    observed = frontier(world.conn, run["id"])
    artifact = create(world.conn, "artifact", project_id=world.project["id"], kind="commit",
        content={"repository_id": str(world.workspace_repo["id"]), "commit_oid": "a" * 40})
    create(world.conn, "publication", artifact_id=artifact["id"], requested_by_assignment_id=root["assignment_id"],
        target={"ref_name": "refs/heads/maintenance-notes"}, status="verified", verified_at=func.now())
    settled = settle_pass(world, root)
    clear_cooldown(world, run)
    change(world.conn, "assignment", settled["id"], status_note="Administrative outcome rewritten")
    assert frontier(world.conn, run["id"]) == observed
    pending = next(row for row in assignments(world, run) if row["automation_id"] and row["status"] == "pending")
    now = world.conn.execute(select(func.now())).scalar_one()
    assert not world.service.readiness(world.conn, dict(pending), now).ready
    assert world.claim()["assignment_id"] == str(child["id"])
    assert world.claim() is None


def test_consumed_explicit_condition_is_not_permanent_recurrence_permission(root_world):
    world = root_world
    run = world.run()
    claim = world.claim()
    child = world.assignment(run, parent_id=claim["assignment_id"])
    now = world.conn.execute(select(func.now())).scalar_one()
    world.assignment(run, parent_id=claim["assignment_id"], not_before=now + timedelta(days=1))
    condition = {"version": 1, "expression": {"op": "status_in",
        "target": {"kind": "assignment", "id": str(child["id"])},
        "values": ["completed", "failed", "cancelled"]}}
    world.command("defer_automation", rule(world.conn, run["id"]), start_condition=condition)
    settle_pass(world, claim)
    clear_cooldown(world, run)
    change(world.conn, "assignment", child["id"], status="completed", finished_at=now)
    pending = next(row for row in assignments(world, run) if row["automation_id"] and row["status"] == "pending")
    # Keep the other worker queued while this maintenance result is consumed.
    change(world.conn, "assignment", pending["id"], queue_rank=-1)
    followup = world.claim()
    assert followup["assignment_id"] == str(pending["id"])
    settle_pass(world, followup)
    clear_cooldown(world, run)
    next_pass = next(row for row in assignments(world, run) if row["automation_id"] and row["status"] == "pending")
    assert not world.service.readiness(world.conn, dict(next_pass), now).ready


def test_recovery_does_not_resurrect_a_consumed_event_condition(root_world):
    world = root_world
    run = world.run()
    claim = world.claim()
    child = world.assignment(run, parent_id=claim["assignment_id"])
    condition = {"version": 1, "expression": {"op": "status_in",
        "target": {"kind": "assignment", "id": str(child["id"])},
        "values": ["completed", "failed", "cancelled"]}}
    world.command("defer_automation", rule(world.conn, run["id"]), start_condition=condition)
    settle_pass(world, claim)
    clear_cooldown(world, run)
    change(world.conn, "assignment", child["id"], status="completed", finished_at=func.now())
    followup = world.claim()
    settle_pass(world, followup)
    clear_cooldown(world, run)
    recovery = world.claim()
    assert len(world.ledger(recovery["assignment_id"])) == 2
    settle_pass(world, recovery)
    clear_cooldown(world, run)
    assert world.claim() is None
    assert len(assignments(world, run)) == 4
    assert get(world.conn, "run", run["id"])["status_note"].startswith("Maintenance blocked:")


@pytest.mark.parametrize("blocked_by", ["unknown_observation", "unavailable_harness"])
def test_idle_unstartable_workers_request_one_diagnosis_on_existing_root(root_world, blocked_by):
    world = root_world
    run = world.run()
    claim = world.claim()
    if blocked_by == "unknown_observation":
        world.assignment(run, parent_id=claim["assignment_id"], start_condition={"version": 1,
            "expression": {"op": "forge_open_count", "project_id": str(world.project["id"]),
                "repository_ids": [str(world.workspace_repo["id"])], "kinds": ["pull_request"], "at_least": 1}})
    else:
        harness = create(world.conn, "harness", slug="unavailable-harness", adapter="codex_exec", adapter_version="1",
            provider_version="1", settings=world.harness["settings"], model_options={})
        world.assignment(run, parent_id=claim["assignment_id"], harness_id=harness["id"])
    settle_pass(world, claim)
    clear_cooldown(world, run)
    pending = next(row for row in assignments(world, run) if row["automation_id"] and row["status"] == "pending")
    assert len(world.ledger(pending["id"])) == 1
    world.scheduler.replenish_maintainers(world.conn, world.actor)
    assert len(world.ledger(pending["id"])) == 2
    assert world.claim()["assignment_id"] == str(pending["id"])
    assert len(assignments(world, run)) == 3


def test_shared_capacity_contention_does_not_request_a_diagnostic_pass(root_world):
    world = root_world
    run = world.run()
    claim = world.claim()
    now = world.conn.execute(select(func.now())).scalar_one()
    child = world.assignment(run, parent_id=claim["assignment_id"], not_before=now + timedelta(days=1))
    settle_pass(world, claim)
    clear_cooldown(world, run)
    pending = next(row for row in assignments(world, run) if row["automation_id"] and row["status"] == "pending")
    other = world.run()
    world.disable_automations(other)
    workers = [world.assignment(other) for _ in range(2)]
    assert {world.claim()["assignment_id"] for _ in workers} == {str(worker["id"]) for worker in workers}
    change(world.conn, "assignment", child["id"], not_before=None)
    world.scheduler.replenish_maintainers(world.conn, world.actor)
    assert len(world.ledger(pending["id"])) == 1
    assert state(world.conn, run["id"])["audit_obligation_id"] is None


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
def test_explicit_owner_wait_survives_spare_capacity_and_wakes_for_all_terminal_results(root_world, terminal):
    world = root_world
    run = world.run()
    claim = world.claim()
    child = world.assignment(run, parent_id=claim["assignment_id"])
    condition = {"version": 1, "expression": {"op": "status_in",
        "target": {"kind": "assignment", "id": str(child["id"])},
        "values": ["completed", "failed", "cancelled"]}}
    world.command("defer_automation", rule(world.conn, run["id"]), start_condition=condition)
    settle_pass(world, claim)
    clear_cooldown(world, run)
    pending = next(row for row in assignments(world, run) if row["automation_id"] and row["status"] == "pending")
    now = world.conn.execute(select(func.now())).scalar_one()
    assert pending["start_condition"] == condition
    assert not world.service.readiness(world.conn, dict(pending), now).ready
    change(world.conn, "assignment", child["id"], status=terminal, finished_at=now)
    assert world.claim()["assignment_id"] == str(pending["id"])


def test_changes_requested_wakes_root_without_a_planner_or_active_model(root_world):
    world = root_world
    run = world.run()
    item = create(world.conn, "forge_item", repository_id=world.document["source_repository_id"],
        origin_run_id=run["id"], remote_number=1, kind="pull_request", title="Milestone proposal",
        status="open", head_commit_oid="b" * 40, observed_at=func.now())
    claim = world.claim()
    reviewer = world.assignment(run, parent_id=claim["assignment_id"])
    settle_pass(world, claim)
    clear_cooldown(world, run)
    pending = next(row for row in assignments(world, run) if row["automation_id"] and row["status"] == "pending")
    now = world.conn.execute(select(func.now())).scalar_one()
    assert not world.service.readiness(world.conn, dict(pending), now).ready
    create(world.conn, "forge_review", forge_item_id=item["id"], remote_id="review-1", reviewer_remote_id="specialist",
        verdict="changes_requested", summary="The statement needs its missing hypothesis", commit_oid=item["head_commit_oid"],
        observed_at=now)
    change(world.conn, "assignment", reviewer["id"], status="completed", finished_at=now)
    assert world.service.readiness(world.conn, dict(pending), now).ready
    assert world.claim()["assignment_id"] == str(pending["id"])


@pytest.mark.parametrize("operator_note", [None, "Operator: preserve this run for benchmark comparison"])
def test_root_failure_has_one_evidence_scoped_recovery_and_new_results_unlock_it(root_world, operator_note):
    world = root_world
    run = world.run()
    if operator_note:
        change(world.conn, "run", run["id"], status_note=operator_note)
    failure = {"kind": "execution", "code": "execution_failed", "message": "Provider could not finish this task"}
    for attempt in (1, 2):
        claim = world.claim()
        assert claim is not None
        failed = world.scheduler.finish(world.conn, world.host_actor,
            get(world.conn, "execution", claim["execution_id"]), "failed", failure)
        assert failed["status"] == "failed"
        assert rule(world.conn, run["id"])["enabled"]
        if attempt == 1:
            assert len(assignments(world, run)) == 2
            watch = state(world.conn, run["id"])
            assert watch["audit_frontier_hash"] == frontier(world.conn, run["id"])
            assert get(world.conn, "obligation", watch["audit_obligation_id"])["kind"] == "decision"
    assert world.claim() is None
    assert len(assignments(world, run)) == 2
    note = get(world.conn, "run", run["id"])["status_note"]
    assert note == operator_note if operator_note else note.startswith("Maintenance blocked:")
    for _ in range(2):
        world.scheduler.tick(world.conn)
    assert len(assignments(world, run)) == 2
    create(world.conn, "forge_item", repository_id=world.document["source_repository_id"],
        origin_run_id=run["id"], remote_number=1, kind="pull_request", title="New evidence",
        status="open", head_commit_oid="c" * 40, observed_at=func.now())
    world.scheduler.tick(world.conn)
    assert world.claim() is not None
    assert len(assignments(world, run)) == 3
    assert get(world.conn, "run", run["id"])["status_note"] == operator_note


def test_transient_root_interruption_reuses_context_without_a_diagnostic_assignment(root_world):
    world = root_world
    run = world.run()
    claim = world.claim()
    pending = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", claim["execution_id"]), "failed",
        {"kind": "transport", "code": "connection_error", "message": "Temporary connection interruption"})
    assert pending["status"] == "pending" and pending["retry_at"]
    assert len(assignments(world, run)) == 1
    assert state(world.conn, run["id"])["audit_obligation_id"] is None
    change(world.conn, "assignment", pending["id"], retry_at=None)
    resumed = world.claim()
    assert resumed["assignment_id"] == claim["assignment_id"]
    assert resumed["provider_thread_record_id"] == claim["provider_thread_record_id"]


def test_worker_unfinished_publication_hands_followup_to_root_maintainer(root_world):
    world = root_world
    run = world.run()
    root = world.claim()
    worker = world.assignment(run, parent_id=root["assignment_id"])
    settle_pass(world, root)
    clear_cooldown(world, run)
    claim = world.claim()
    assert claim["assignment_id"] == str(worker["id"])
    artifact = create(world.conn, "artifact", project_id=world.project["id"], kind="commit",
        content={"repository_id": str(world.workspace_repo["id"]), "commit_oid": "d" * 40})
    create(world.conn, "publication", artifact_id=artifact["id"], requested_by_assignment_id=worker["id"],
        target={"ref_name": "refs/heads/proposal"}, status="verified", verified_at=func.now())
    settled = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", claim["execution_id"]), "succeeded")
    assert settled["status"] == "completed"
    assert world.ledger(worker["id"])[0]["status"] == "superseded"
    owner = world.claim()
    assert owner["role"] == "maintainer" and owner["functions"] == []
    assert len(world.ledger(owner["assignment_id"])) == 2


@pytest.mark.parametrize("outcome", ["failed", "published"])
def test_planner_specialty_does_not_take_root_recovery_ownership(root_world, outcome):
    world = root_world
    run = world.run()
    root = world.claim()
    worker = world.assignment(run, parent_id=root["assignment_id"])
    mission = world.service.mission(world.conn, world.actor, models.MissionCreate(
        project_id=world.project["id"], parent_id=world.mission["id"],
        expected_parent_revision=get(world.conn, "mission", world.mission["id"])["revision"],
        title="Local proof route", objective="Plan only the auxiliary estimate",
        acceptance_criteria=["Identify an applicable estimate"], delegation_note="Root maintenance retains global recovery"))
    now = world.conn.execute(select(func.now())).scalar_one()
    planner = world.service.assignment(world.conn, world.actor, models.AssignmentCreate(
        run_id=run["id"], mission_id=mission["id"], parent_id=root["assignment_id"], functions=["planner"],
        not_before=now + timedelta(days=1)))
    settle_pass(world, root)
    clear_cooldown(world, run)
    claim = world.claim()
    assert claim["assignment_id"] == str(worker["id"])
    if outcome == "published":
        artifact = create(world.conn, "artifact", project_id=world.project["id"], kind="commit",
            content={"repository_id": str(world.workspace_repo["id"]), "commit_oid": "e" * 40})
        create(world.conn, "publication", artifact_id=artifact["id"], requested_by_assignment_id=worker["id"],
            target={"ref_name": "refs/heads/repair"}, status="verified", verified_at=now)
    world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", claim["execution_id"]), "succeeded" if outcome == "published" else "failed")
    assert len(world.ledger(planner["id"])) == 1
    owner = next(row for row in assignments(world, run) if row["automation_id"] and row["status"] == "pending")
    assert len(world.ledger(owner["id"])) == 2


def test_semantic_root_completion_drains_and_settles_without_another_agent(root_world):
    world = root_world
    run = world.run()
    claim = world.claim()
    world.command("complete_mission", get(world.conn, "mission", run["mission_id"]),
        note="The phase deliverable and required acceptance evidence are complete")
    settled = settle_pass(world, claim)
    assert settled["status"] == "completed"
    assert get(world.conn, "run", run["id"])["status"] == "draining"
    assert world.scheduler.tick(world.conn)["completed"] == 1
    assert get(world.conn, "run", run["id"])["status"] == "completed"
    assert len(assignments(world, run)) == 1
