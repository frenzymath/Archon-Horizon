from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, text, update

from archon_horizon.pipeline import models
from archon_horizon.pipeline.persistence.artifacts import ArtifactStore
from archon_horizon.pipeline.auth import Actor, authenticate, live_execution
from archon_horizon.pipeline.config import PipelineConfig
from archon_horizon.pipeline.commands import Command, execute as execute_command
from archon_horizon.pipeline.persistence.database import Database
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.persistence.records import create, get, snapshot, transaction_lock
from archon_horizon.pipeline.execution.scheduler import Scheduler
from archon_horizon.pipeline.persistence.schema import tables
from archon_horizon.pipeline.missions.service import Service
from archon_horizon.pipeline.execution.worker_events import WorkerOperation, handle as handle_worker_operation


class HistoricalScheduler(Scheduler):
    """Seed persisted legacy profiles for lifecycle compatibility fixtures."""

    @staticmethod
    def validate_launch_profile(data):
        # These fixtures reconstruct historical persisted runs. Production
        # Scheduler rejects newly requested orchestrator profiles separately.
        pass

    @staticmethod
    def automation_specs(run, phase, project_id, phase_repository_ids):
        if phase.get("orchestrated"):
            return Scheduler.automation_specs(run, phase, project_id, phase_repository_ids)
        return (
            ("planner", "worker", ["planner"],
             "Plan the next bounded work frontier and concrete repairs, then finish.",
             {"version": 1, "expression": {"op": "planning_needed", "run_id": str(run["id"])}}, True),
            ("maintainer", "maintainer", [],
             "Review and integrate a bounded batch of current Forge work, then finish.",
             {"version": 1, "expression": {"op": "forge_actionable_count", "project_id": str(project_id),
                 "repository_ids": phase_repository_ids, "kinds": ["pull_request", "issue"],
                 "labels": [], "match": "any", "at_least": 1}}, True),
        )


@pytest.fixture(scope="module")
def service_database():
    url = os.environ.get("HORIZON_PIPELINE_TEST_URL")
    if not url:
        pytest.skip("set HORIZON_PIPELINE_TEST_URL to an isolated PostgreSQL test database")
    schema = "pipeline_service_" + uuid4().hex
    db = Database(url, schema=schema)
    db.migrate()
    try:
        yield db
    finally:
        with db.engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        db.close()


@dataclass
class World:
    conn: object
    service: Service
    scheduler: Scheduler
    actor: Actor
    host_actor: Actor
    project: dict
    mission: dict
    host: dict
    harness: dict
    document: dict
    workspace_repo: dict
    policy: dict

    def run(self, **kwargs):
        orchestrated = kwargs.pop("orchestrated", False)
        row = self.scheduler.run(self.conn, self.actor, models.RunCreate(
            mission_id=self.mission["id"],
            phase={"kind": "preprocessing", "roadmap_document_id": self.document["id"],
                   "orchestrated": orchestrated},
            host_ids=[self.host["id"]], orchestration=kwargs.pop("orchestration", "legacy"), **kwargs))
        self.mission = get(self.conn, "mission", self.mission["id"])
        return row

    def assignment(self, run, **kwargs):
        return self.service.assignment(self.conn, self.actor, models.AssignmentCreate(
            run_id=run["id"], mission_id=self.mission["id"], **kwargs))

    def claim(self):
        return self.scheduler.claim(self.conn, self.host_actor, self.host["id"], [self.harness["id"]])

    def command(self, operation, row, **args):
        return execute_command(self.conn, self.actor, Command(operation=operation,
            target_id=row["id"], expected_revision=row["revision"], args=args), self.service, self.scheduler)

    def disable_automations(self, run):
        self.conn.execute(update(tables["automation"]).where(tables["automation"].c.run_id == run["id"]).values(enabled=False))
        self.conn.execute(update(tables["assignment"]).where(tables["assignment"].c.run_id == run["id"],
            tables["assignment"].c.automation_id.is_not(None)).values(status="cancelled"))

    def ledger(self, assignment_id):
        return [dict(row) for row in self.conn.execute(select(tables["obligation"]).where(
            tables["obligation"].c.assignment_id == assignment_id).order_by(tables["obligation"].c.number)).mappings()]


def make_world(conn, tmp_path):
    suffix = uuid4().hex[:12]
    principal = create(conn, "principal", kind="human", display_name="Operator", username="operator_" + suffix)
    conn.execute(insert(tables["system_grant"]).values(principal_id=principal["id"], permission="administer_installation"))
    actor = Actor(principal["id"], "human", {"username": principal["username"]}, "api_key")
    config = PipelineConfig(database_url=os.environ["HORIZON_PIPELINE_TEST_URL"], state_root=tmp_path, lease_seconds=30,
        storage={"minimum_free_bytes": 0, "warn_used_percent": 98, "pause_used_percent": 99})
    service = Service(ArtifactStore(config.artifact_root), config)
    scheduler = HistoricalScheduler(service)
    project = service.project(conn, actor, models.ProjectCreate(slug="project_" + suffix, title="Test formalization", workflow="legacy"))
    integration = create(conn, "integration", kind="forge", endpoint="https://forge.invalid", credential_ref="secret:test")
    workspace_repo = create(conn, "repository", project_id=project["id"], slug="workspace", integration_id=integration["id"],
                            remote_id="workspace", default_branch="main", purpose="workspace")
    roadmap_repo = create(conn, "repository", project_id=project["id"], slug="roadmap", integration_id=integration["id"],
                          remote_id="roadmap", default_branch="main", purpose="knowledge")
    document = create(conn, "document", project_id=project["id"], number=1, kind="roadmap", title="Roadmap",
                      source_repository_id=roadmap_repo["id"], source_path="roadmap.md", source_commit_oid="a" * 40)
    policy = create(conn, "review_policy", project_id=project["id"], slug="roadmap", phases=["preprocessing", "formalization"],
                    instructions="Review roadmap statements proportionately")
    conn.execute(insert(tables["review_policy_repository"]).values(review_policy_id=policy["id"], repository_id=roadmap_repo["id"]))
    mission = service.mission(conn, actor, models.MissionCreate(project_id=project["id"], title="Main theorem",
                              objective="Prove the target faithfully", roadmap_document_id=document["id"]))
    host = create(conn, "host", slug="host_" + suffix, display_name="Local", workspace_root=str(tmp_path / "workspaces"),
                  scratch_root=str(tmp_path / "scratch"), sandbox={"schema_version": 1, "mode": "unrestricted"}, heartbeat_at=func.now())
    harness = create(conn, "harness", slug="harness_" + suffix, adapter="codex_exec", adapter_version="1", provider_version="1",
                     model_options={"model": "test-model"}, settings=models.AdapterSettings().model_dump())
    conn.execute(insert(tables["host_harness"]).values(host_id=host["id"], harness_id=harness["id"],
        executable_path="/bin/codex", provider_home=str(tmp_path / "provider"), credential_ref="secret:provider",
        execution_slots=2, max_parallel_subagents=3))
    host_principal = create(conn, "principal", kind="host", display_name="Local daemon", host_id=host["id"])
    host_actor = Actor(host_principal["id"], "host", {"host_id": str(host["id"])}, "host_key")
    for number in range(3):
        create(conn, "workspace", project_id=project["id"], host_id=host["id"], repository_id=workspace_repo["id"],
               path=str(tmp_path / f"workspace-{number}"), branch_name=f"worker-{number}", base_commit_oid="a" * 40, status="ready")
    return World(conn, service, scheduler, actor, host_actor, project, mission, host, harness, document, workspace_repo, policy)


@pytest.fixture
def world(service_database, tmp_path):
    with service_database.engine.connect() as conn:
        transaction = conn.begin()
        try:
            transaction_lock(conn)
            yield make_world(conn, tmp_path)
        finally:
            transaction.rollback()


def test_historical_run_has_one_planner_and_one_waiting_maintainer(world):
    run = world.run()
    assignments = list(world.conn.execute(select(tables["assignment"]).where(tables["assignment"].c.run_id == run["id"])).mappings())
    assert len(assignments) == 2
    assert {item["role"] for item in assignments} == {"worker", "maintainer"}
    now = datetime.now(timezone.utc)
    planner = next(item for item in assignments if "planner" in item["functions"])
    maintainer = next(item for item in assignments if item["role"] == "maintainer")
    assert world.service.readiness(world.conn, planner, now).ready
    assert not world.service.readiness(world.conn, maintainer, now).ready
    assert "Forge" in world.service.readiness(world.conn, maintainer, now).reason
    for automation in world.conn.execute(select(tables["automation"])).mappings():
        assert world.scheduler.replenish(world.conn, world.actor, dict(automation)) is None


def test_orchestrated_run_starts_control_only_and_enables_one_profile_batch(world):
    run = world.run(orchestrated=True)
    automations = list(world.conn.execute(select(tables["automation"]).where(
        tables["automation"].c.run_id == run["id"]).order_by(tables["automation"].c.name)).mappings())
    assert {item["name"] for item in automations} == {"maintainer", "orchestrator", "planner"}
    assert {item["name"] for item in automations if item["enabled"]} == {"orchestrator"}
    assert all(item["instructions"] for item in automations)
    assert "control pass" in next(item for item in automations if item["name"] == "orchestrator")["instructions"]
    assignments = list(world.conn.execute(select(tables["assignment"]).where(
        tables["assignment"].c.run_id == run["id"])).mappings())
    assert len(assignments) == 1
    assert assignments[0]["functions"] == ["orchestrator"]
    assert assignments[0]["instructions"] == next(item for item in automations if item["name"] == "orchestrator")["instructions"]

    planner = next(item for item in automations if item["name"] == "planner")
    world.command("defer_automation", planner, enabled=True)
    assignments = list(world.conn.execute(select(tables["assignment"]).where(
        tables["assignment"].c.automation_id == planner["id"])).mappings())
    assert len(assignments) == 1
    assert assignments[0]["functions"] == ["planner"]
    # A scheduler tick or a repeated reconciliation cannot multiply this
    # recurrence while its first assignment is pending.
    world.scheduler.replenish_planners(world.conn, world.actor)
    assert world.conn.execute(select(func.count()).select_from(tables["assignment"]).where(
        tables["assignment"].c.automation_id == planner["id"],
        tables["assignment"].c.status.in_(("pending", "running", "stopping")))).scalar_one() == 1


def test_maintainer_replenishment_does_not_bypass_orchestrator_cooldown(world):
    run = world.run(orchestrated=True)
    assignment, automation = tables["assignment"], tables["automation"]
    rule = world.conn.execute(select(automation).where(
        automation.c.run_id == run["id"], automation.c.name == "orchestrator")).mappings().one()
    current = world.conn.execute(select(assignment).where(
        assignment.c.automation_id == rule["id"])).mappings().one()
    now = world.conn.execute(select(func.now())).scalar_one()
    world.conn.execute(update(assignment).where(assignment.c.id == current["id"]).values(
        status="completed", finished_at=now))

    assert world.scheduler.replenish_maintainers(world.conn, world.actor) == 0
    assert world.scheduler.tick(world.conn)["orchestrator_episodes"] == 0
    assert world.conn.execute(select(func.count()).select_from(assignment).where(
        assignment.c.automation_id == rule["id"])).scalar_one() == 1

    world.conn.execute(update(assignment).where(assignment.c.id == current["id"]).values(
        finished_at=now - timedelta(seconds=rule["cooldown_seconds"] + 1)))
    assert world.scheduler.tick(world.conn)["orchestrator_episodes"] == 1
    assert world.scheduler.tick(world.conn)["orchestrator_episodes"] == 0
    assert world.conn.execute(select(func.count()).select_from(assignment).where(
        assignment.c.automation_id == rule["id"],
        assignment.c.status.in_(("pending", "running", "stopping")))).scalar_one() == 1


@pytest.mark.parametrize("unlock", ["new_head", "superseding_approval"])
def test_maintainer_waits_for_new_evidence_after_changes_requested(world, unlock):
    run = world.run()
    maintainer = world.conn.execute(select(tables["assignment"]).where(
        tables["assignment"].c.run_id == run["id"], tables["assignment"].c.role == "maintainer")).mappings().one()
    repository = get(world.conn, "repository", world.document["source_repository_id"])
    create(world.conn, "connector_cursor", integration_id=repository["integration_id"],
        consumer="forge:" + str(repository["id"]), status="current", last_synced_at=func.now())
    item = create(world.conn, "forge_item", repository_id=repository["id"], remote_number=1,
        kind="pull_request", title="Milestone route", status="open", labels=[],
        head_commit_oid="a" * 40, target_branch="main", observed_at=func.now())
    now = datetime.now(timezone.utc)
    assert world.service.readiness(world.conn, maintainer, now).ready
    create(world.conn, "forge_review", forge_item_id=item["id"], remote_id="review-1",
        reviewer_remote_id="reviewer", verdict="changes_requested", summary="Repair the route",
        commit_oid="a" * 40, observed_at=func.now())
    waiting = world.service.readiness(world.conn, maintainer, now)
    assert not waiting.ready
    assert "actionable" in waiting.reason
    # The literal open-count condition remains useful for explicit repair work.
    literal = {**dict(maintainer), "start_condition": {"version": 1, "expression": {
        **maintainer["start_condition"]["expression"], "op": "forge_open_count"}}}
    assert world.service.readiness(world.conn, literal, now).ready
    if unlock == "new_head":
        from archon_horizon.pipeline.persistence.records import change
        change(world.conn, "forge_item", item["id"], head_commit_oid="b" * 40)
    else:
        create(world.conn, "forge_review", forge_item_id=item["id"], remote_id="review-2",
            reviewer_remote_id="reviewer", verdict="approved", summary="The objection is resolved",
            commit_oid="a" * 40, observed_at=now + timedelta(seconds=1))
    assert world.service.readiness(world.conn, maintainer, now).ready


def test_admission_rejects_two_nominal_slots_behind_one_account_call(world):
    limit = create(world.conn, "resource_limit", kind="provider_account", slug="one-account", max_concurrent=1)
    world.conn.execute(insert(tables["host_harness_limit"]).values(host_id=world.host["id"], harness_id=world.harness["id"], resource_limit_id=limit["id"]))
    with pytest.raises(DomainError) as error:
        world.run()
    assert error.value.code == "insufficient_capacity"


def test_disabled_automation_blocks_existing_pending_occurrence(world):
    run = world.run()
    row = world.conn.execute(select(tables["assignment"]).where(
        tables["assignment"].c.run_id == run["id"], tables["assignment"].c.functions.contains(["planner"]))).mappings().one()
    rule = get(world.conn, "automation", row["automation_id"])
    world.command("defer_automation", rule, enabled=False)
    assert not world.service.readiness(world.conn, dict(row), datetime.now(timezone.utc)).ready
    assert world.claim() is None


def test_idle_recheck_wakes_only_planner_and_retains_recurring_rule(world):
    from archon_horizon.pipeline.persistence.records import change
    run = world.run()
    now = world.conn.execute(select(func.now())).scalar_one()
    a, au = tables["assignment"], tables["automation"]
    planner = world.conn.execute(select(a).where(a.c.run_id == run["id"], a.c.functions.contains(["planner"]))).mappings().one()
    rule = get(world.conn, "automation", planner["automation_id"])
    blocked = {"version": 1, "expression": {"op": "queue_below", "run_id": str(run["id"]), "count": 0}}
    world.command("defer_automation", rule, start_condition=blocked)
    blocked = get(world.conn, "automation", rule["id"])["start_condition"]
    now += timedelta(minutes=6)
    assert world.scheduler.recheck_idle_runs(world.conn, world.actor, now) == 1
    awakened = get(world.conn, "assignment", planner["id"])
    assert awakened["start_condition"] is None and awakened["not_before"] is None
    assert get(world.conn, "automation", rule["id"])["start_condition"] == blocked
    assert sum(item["description"].startswith("Idle queue recheck:") for item in world.ledger(planner["id"])) == 1
    assert world.scheduler.recheck_idle_runs(world.conn, world.actor, now) == 0
    # Repeating the same wait needs substantive progress, not another timer tick.
    change(world.conn, "assignment", planner["id"], start_condition=blocked)
    assert world.scheduler.recheck_idle_runs(world.conn, world.actor, now) == 0
    rule = get(world.conn, "automation", rule["id"])
    world.command("defer_automation", rule, no_progress=False)
    assert get(world.conn, "automation", rule["id"])["no_progress_count"] == 0


@pytest.mark.parametrize("guard", ["paused", "disabled", "ready_work", "budget", "retry", "offline", "expiry"])
def test_idle_recheck_respects_admission_guards(world, guard):
    run = world.run()
    now = world.conn.execute(select(func.now())).scalar_one()
    a, au = tables["assignment"], tables["automation"]
    planner = world.conn.execute(select(a).where(a.c.run_id == run["id"], a.c.functions.contains(["planner"]))).mappings().one()
    world.conn.execute(update(a).where(a.c.id == planner["id"]).values(start_condition={"version": 1,
        "expression": {"op": "queue_below", "run_id": str(run["id"]), "count": 0}}))
    now += timedelta(minutes=6)
    if guard == "paused":
        world.conn.execute(update(tables["run"]).where(tables["run"].c.id == run["id"]).values(status="paused"))
    elif guard == "disabled":
        world.conn.execute(update(au).where(au.c.id == planner["automation_id"]).values(enabled=False))
    elif guard == "ready_work":
        world.assignment(run)
    elif guard == "budget":
        world.conn.execute(update(tables["run"]).where(tables["run"].c.id == run["id"]).values(token_budget=0))
    elif guard == "retry":
        world.conn.execute(update(a).where(a.c.id == planner["id"]).values(retry_at=now+timedelta(hours=1)))
    elif guard == "expiry":
        world.conn.execute(update(a).where(a.c.id == planner["id"]).values(not_before=None, expires_at=now-timedelta(seconds=1)))
    else:
        world.conn.execute(update(tables["host"]).where(tables["host"].c.id == world.host["id"]).values(mode="disabled"))
    assert world.scheduler.recheck_idle_runs(world.conn, world.actor, now) == 0


def test_postprocessing_maintainer_ignores_other_repository_backlogs(world):
    library = create(world.conn, "repository", project_id=world.project["id"], slug="fresh-library",
        integration_id=world.workspace_repo["integration_id"], remote_id="fresh-library",
        default_branch="main", purpose="library")
    policy = create(world.conn, "review_policy", project_id=world.project["id"], slug="library",
        phases=["postprocessing"], instructions="Review library contributions")
    world.conn.execute(insert(tables["review_policy_repository"]).values(
        review_policy_id=policy["id"], repository_id=library["id"]))
    workspace = world.conn.execute(select(tables["workspace"]).where(
        tables["workspace"].c.project_id == world.project["id"])).mappings().first()
    run = world.scheduler.run(world.conn, world.actor, models.RunCreate(orchestration="legacy",
        mission_id=world.mission["id"], host_ids=[world.host["id"]],
        phase={"kind": "postprocessing", "source_workspace_id": workspace["id"],
               "source_commit_oid": "a" * 40, "target_repository_id": library["id"]}))
    maintainer = world.conn.execute(select(tables["assignment"]).where(
        tables["assignment"].c.run_id == run["id"], tables["assignment"].c.role == "maintainer")).mappings().one()
    create(world.conn, "connector_cursor", integration_id=library["integration_id"], consumer="forge:" + str(library["id"]),
        status="current", last_synced_at=func.now())
    create(world.conn, "connector_cursor", integration_id=library["integration_id"], consumer="forge",
        status="unavailable", last_synced_at=func.now())
    create(world.conn, "connector_cursor", integration_id=world.workspace_repo["integration_id"],
        consumer="forge:" + str(world.workspace_repo["id"]), status="unavailable", last_synced_at=func.now())
    create(world.conn, "forge_item", repository_id=world.workspace_repo["id"], remote_number=1,
        kind="issue", title="Unrelated backlog", status="open", labels=[], observed_at=func.now())
    now = datetime.now(timezone.utc)
    assert not world.service.readiness(world.conn, maintainer, now).ready
    create(world.conn, "forge_item", repository_id=library["id"], remote_number=1,
        kind="issue", title="Fresh library work", status="open", labels=[], observed_at=func.now())
    assert world.service.readiness(world.conn, maintainer, now).ready


def test_claim_uses_shared_slots_and_skips_ineligible_queue_entries(world):
    run = world.run()
    world.disable_automations(run)
    delayed = world.assignment(run, not_before=datetime.now(timezone.utc) + timedelta(days=1))
    first = world.assignment(run)
    urgent_maintainer = world.assignment(run, role="maintainer")
    world.assignment(run)
    first_claim = world.claim()
    second_claim = world.claim()
    assert UUID(first_claim["assignment_id"]) == first["id"]
    assert UUID(second_claim["assignment_id"]) == urgent_maintainer["id"]
    assert world.claim() is None
    assert get(world.conn, "assignment", delayed["id"])["status"] == "pending"


def test_lease_cancellation_revokes_agent_authority_without_losing_obligations(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    agent = authenticate(world.conn, claim["execution_token"])
    assert live_execution(world.conn, agent)["assignment_id"] == assignment["id"]
    current = get(world.conn, "assignment", assignment["id"])
    stopped = world.scheduler.cancel(world.conn, world.actor, current, "Operator cancelled")
    assert stopped["status"] == "stopping"
    with pytest.raises(DomainError) as error:
        live_execution(world.conn, agent)
    assert error.value.code == "stale_epoch"
    heartbeat = world.scheduler.heartbeat(world.conn, world.host_actor, UUID(claim["execution_id"]), claim["epoch"])
    assert heartbeat["stop"] is True
    execution = get(world.conn, "execution", claim["execution_id"])
    finished = world.scheduler.finish(world.conn, world.host_actor, execution, "cancelled")
    assert finished["status"] == "cancelled"
    assert world.ledger(assignment["id"])[0]["status"] == "open"
    with pytest.raises(DomainError):
        authenticate(world.conn, claim["execution_token"])


def test_transient_failure_retries_same_assignment_and_provider_context(world):
    run = world.run(retry_policy=models.RetryPolicy(max_recovery_attempts=1))
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    first_execution = get(world.conn, "execution", claim["execution_id"])
    result = world.scheduler.finish(world.conn, world.host_actor, first_execution, "failed",
        {"kind": "transport", "code": "connection_error", "message": "Connection interrupted"})
    assert result["status"] == "pending"
    assert result["retry_at"] > datetime.now(timezone.utc)
    assert world.claim() is None
    world.conn.execute(update(tables["assignment"]).where(tables["assignment"].c.id == assignment["id"]).values(retry_at=None))
    second = world.claim()
    assert second["epoch"] == claim["epoch"] + 1
    assert second["provider_thread_record_id"] == claim["provider_thread_record_id"]
    final = world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", second["execution_id"]), "failed",
        {"kind": "transport", "code": "connection_error", "message": "Connection interrupted again"})
    assert final["status"] == "failed"
    assert get(world.conn, "mission", world.mission["id"])["status"] == "open"


def test_automated_deadline_is_terminal_until_explicit_operator_retry(world):
    run = world.run(orchestrated=True)
    orchestrator = world.conn.execute(select(tables["automation"]).where(
        tables["automation"].c.run_id == run["id"],
        tables["automation"].c.name == "orchestrator")).mappings().one()
    claim = world.claim()
    result = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", claim["execution_id"]), "failed",
        {"kind": "execution", "code": "request_deadline",
         "message": "Provider request exceeded its execution deadline"})
    assert result["status"] == "failed"
    assert "execution deadline" in result["status_note"]
    assert get(world.conn, "automation", orchestrator["id"])["enabled"] is False
    assert world.claim() is None
    retried = world.command("retry_assignment", result)
    assert retried["status"] == "pending"
    assert get(world.conn, "automation", orchestrator["id"])["enabled"] is True


@pytest.mark.parametrize("reason", ["execution_budget_reached", "request_budget_reached"])
@pytest.mark.parametrize("accounted", [False, True])
def test_episode_budget_receipt_does_not_restart_unfinished_context(world, reason, accounted):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    grant = world.claim()
    if accounted:
        obligation = world.ledger(assignment["id"])[0]
        world.service.resolve_obligation(world.conn, world.actor, obligation["id"], models.ObligationResolve(
            expected_revision=obligation["revision"], status="done",
            resolution={"kind": "completed", "note": "Deliverable independently accounted", "evidence": []}))
    receipt = WorkerOperation(operation_id=uuid4(), execution_id=grant["execution_id"], epoch=grant["epoch"],
        kind="execution_finished", payload={"status": "yielded", "reason": reason},
        occurred_at=datetime.now(timezone.utc).timestamp())
    result = handle_worker_operation(world.conn, world.host_actor, receipt, world.service, world.scheduler)
    assert result["status"] == ("completed" if accounted else "failed")
    assert world.claim() is None
    if not accounted:
        assert world.ledger(assignment["id"])[0]["status"] == "open"
        assert "explicit replanning" in get(world.conn, "assignment", assignment["id"])["status_note"]


def test_assignment_completion_requires_ledger_but_failure_does_not(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    waiting = world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", claim["execution_id"]), "succeeded")
    assert waiting["status"] == "pending"
    assert "open obligations" in waiting["status_note"]
    obligation = world.ledger(assignment["id"])[0]
    world.service.resolve_obligation(world.conn, world.actor, obligation["id"], models.ObligationResolve(
        expected_revision=obligation["revision"], status="done", resolution={"kind": "completed", "note": "The intended work is complete", "evidence": []}))
    world.conn.execute(update(tables["assignment"]).where(tables["assignment"].c.id == assignment["id"]).values(not_before=None))
    next_claim = world.claim()
    completed = world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", next_claim["execution_id"]), "succeeded")
    assert completed["status"] == "completed"


def test_maintainer_can_reconcile_open_ledger_of_completed_assignment(world):
    run = world.run()
    world.disable_automations(run)
    target = world.assignment(run)
    owner = world.assignment(run, role="maintainer")
    obligation = world.ledger(target["id"])[0]

    # Model the historical state that stranded the preprocessing runs: the
    # provider assignment settled before its final obligation was accounted.
    world.conn.execute(update(tables["assignment"]).where(
        tables["assignment"].c.id == target["id"]).values(
            status="completed", finished_at=func.now()))
    claim = world.claim()
    assert UUID(claim["assignment_id"]) == owner["id"]
    maintainer = authenticate(world.conn, claim["execution_token"])

    resolved = world.service.resolve_obligation(world.conn, maintainer, obligation["id"],
        models.ObligationResolve(expected_revision=obligation["revision"], status="done",
            resolution={"kind": "completed", "note": "Historical completed assignment reconciled",
                        "evidence": [{"kind": "document", "id": str(world.document["id"])}]}))
    assert resolved["status"] == "done"


def test_worker_cannot_reconcile_another_completed_assignment_ledger(world):
    run = world.run()
    world.disable_automations(run)
    target = world.assignment(run)
    owner = world.assignment(run)
    obligation = world.ledger(target["id"])[0]
    world.conn.execute(update(tables["assignment"]).where(
        tables["assignment"].c.id == target["id"]).values(
            status="completed", finished_at=func.now()))
    claim = world.claim()
    assert UUID(claim["assignment_id"]) == owner["id"]
    worker = authenticate(world.conn, claim["execution_token"])
    with pytest.raises(DomainError) as error:
        world.service.resolve_obligation(world.conn, worker, obligation["id"],
            models.ObligationResolve(expected_revision=obligation["revision"], status="done",
                resolution={"kind": "completed", "note": "Unauthorized settlement", "evidence": []}))
    assert error.value.code == "forbidden"
    assert get(world.conn, "obligation", obligation["id"])["status"] == "open"


def test_delegation_cycles_are_rejected_and_cancelled_delegate_reopens_accounting(world):
    run = world.run()
    world.disable_automations(run)
    first, second = world.assignment(run), world.assignment(run)
    first_obligation, second_obligation = world.ledger(first["id"])[0], world.ledger(second["id"])[0]
    world.service.resolve_obligation(world.conn, world.actor, first_obligation["id"], models.ObligationResolve(
        expected_revision=1, status="handled", resolution={"kind": "delegated", "note": "Second worker owns the proof", "assignment_ids": [second["id"]]}))
    with pytest.raises(DomainError) as error:
        world.service.resolve_obligation(world.conn, world.actor, second_obligation["id"], models.ObligationResolve(
            expected_revision=1, status="handled", resolution={"kind": "delegated", "note": "Send it back", "assignment_ids": [first["id"]]}))
    assert error.value.code == "dependency_cycle"
    world.scheduler.cancel(world.conn, world.actor, second, "Cannot continue")
    assert get(world.conn, "obligation", first_obligation["id"])["status"] == "open"


def test_expired_lease_is_fenced_before_capacity_is_reused(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    execution = tables["execution"]
    # Match the scheduler's transaction timestamp, even when fixture setup is slow.
    world.conn.execute(update(execution).where(execution.c.id == UUID(claim["execution_id"])).values(
        lease_expires_at=func.now() - timedelta(seconds=1)))
    with pytest.raises(DomainError) as error:
        world.scheduler.heartbeat(world.conn, world.host_actor, UUID(claim["execution_id"]), claim["epoch"])
    assert error.value.code == "stale_epoch"
    outcome = world.scheduler.tick(world.conn)
    assert outcome["recovered"] == 1
    assert get(world.conn, "execution", claim["execution_id"])["status"] == "lost"
    assert get(world.conn, "assignment", assignment["id"])["status"] == "pending"


def test_provider_account_cap_is_enforced_and_released_on_failure(world):
    hh = tables["host_harness"]
    world.conn.execute(update(hh).where(hh.c.host_id == world.host["id"]).values(execution_slots=3))
    limit = create(world.conn, "resource_limit", kind="provider_account", slug="shared-provider", max_concurrent=2)
    world.conn.execute(insert(tables["host_harness_limit"]).values(host_id=world.host["id"],
        harness_id=world.harness["id"], resource_limit_id=limit["id"]))
    run = world.run()
    world.disable_automations(run)
    for _ in range(3):
        world.assignment(run)
    first, second = world.claim(), world.claim()
    assert first is not None and second is not None
    assert world.claim() is None
    world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", first["execution_id"]), "failed",
        {"kind": "execution", "code": "execution_failed", "message": "Provider stopped"})
    assert world.claim() is not None


def test_blocked_front_of_large_queue_does_not_starve_ready_work(world):
    run = world.run()
    world.disable_automations(run)
    condition = {"version": 1, "expression": {"op": "status_in", "target": {
        "kind": "mission", "id": str(world.mission["id"])}, "values": ["completed"]}}
    world.conn.execute(insert(tables["assignment"]), [
        {"run_id": run["id"], "mission_id": world.mission["id"], "number": number,
         "queue_rank": number * 1024, "start_condition": condition}
        for number in range(3, 264)
    ])
    ready = world.assignment(run)
    claim = world.claim()
    assert claim is not None
    assert UUID(claim["assignment_id"]) == ready["id"]


def test_configuration_failure_waits_for_operator_repair_without_new_assignment(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    result = world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", claim["execution_id"]), "failed",
        {"kind": "configuration", "code": "authentication_failed", "message": "Provider authentication expired"})
    assert result["status"] == "pending"
    hh = tables["host_harness"]
    assert world.conn.execute(select(hh.c.enabled).where(hh.c.host_id == world.host["id"], hh.c.harness_id == world.harness["id"])).scalar_one() is False
    assert world.claim() is None
    assert world.ledger(assignment["id"])[0]["status"] == "open"


def test_concurrent_host_claims_cannot_duplicate_work_or_exceed_capacity(service_database, tmp_path):
    with service_database.transaction() as conn:
        transaction_lock(conn)
        configured = make_world(conn, tmp_path)
        run = configured.run()
        configured.disable_automations(run)
        for _ in range(8):
            configured.assignment(run)
        host_actor, host_id, harness_id = configured.host_actor, configured.host["id"], configured.harness["id"]
        scheduler = configured.scheduler

    def claim_once(_):
        with service_database.transaction() as conn:
            transaction_lock(conn)
            return scheduler.claim(conn, host_actor, host_id, [harness_id])

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = [result for result in executor.map(claim_once, range(4)) if result is not None]
    assert len(results) == 2
    assert len({result["assignment_id"] for result in results}) == 2
    assert len({result["workspace_id"] for result in results}) == 2


def test_business_snapshot_includes_normalized_review_relations(world):
    descriptor = create(world.conn, "reviewer_descriptor", project_id=world.project["id"], slug="statement",
                        instructions="Inspect theorem hypotheses and conclusion")
    world.conn.execute(insert(tables["review_policy_reviewer"]).values(review_policy_id=world.policy["id"], reviewer_descriptor_id=descriptor["id"]))
    revision_id = snapshot(world.conn, "review_policy", world.policy, world.actor.id)
    pinned = get(world.conn, "record_revision", revision_id)["content"]
    assert pinned["reviewer_descriptor_ids"] == [str(descriptor["id"])]
    assert pinned["repository_ids"] == [str(world.document["source_repository_id"])]


def test_resuming_context_retains_original_harness_revision(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    old_execution = get(world.conn, "execution", claim["execution_id"])
    world.scheduler.finish(world.conn, world.host_actor, old_execution, "failed",
        {"kind": "transport", "code": "connection_error", "message": "Connection interrupted"})
    harness = tables["harness"]
    world.conn.execute(update(harness).where(harness.c.id == world.harness["id"]).values(
        model_options={"model": "new-default"}, provider_version="2"))
    world.conn.execute(update(tables["assignment"]).where(tables["assignment"].c.id == assignment["id"]).values(retry_at=None))
    resumed = world.claim()
    assert resumed is not None
    resumed_execution = get(world.conn, "execution", resumed["execution_id"])
    assert resumed_execution["harness_revision_id"] == old_execution["harness_revision_id"]
    assert resumed["provider_thread_record_id"] == claim["provider_thread_record_id"]


def test_obligation_replacement_cycle_cannot_account_for_unfinished_work(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    first = world.ledger(assignment["id"])[0]
    second = world.service.obligation(world.conn, world.actor, models.ObligationCreate(
        assignment_id=assignment["id"], description="Alternative formulation of the missing proof"))
    world.service.resolve_obligation(world.conn, world.actor, first["id"], models.ObligationResolve(
        expected_revision=1, status="superseded", resolution={"kind": "superseded", "note": "Reframed as another obligation",
            "replacement_obligation_ids": [second["id"]]}))
    with pytest.raises(DomainError) as error:
        world.service.resolve_obligation(world.conn, world.actor, second["id"], models.ObligationResolve(
            expected_revision=1, status="superseded", resolution={"kind": "superseded", "note": "Refer back to the earlier formulation",
                "replacement_obligation_ids": [first["id"]]}))
    assert error.value.code == "dependency_cycle"


def test_unavailable_context_never_silently_restarts_from_zero(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", claim["execution_id"]), "failed",
        {"kind": "transport", "code": "connection_error", "message": "Connection interrupted"})
    thread = tables["provider_thread"]
    world.conn.execute(update(thread).where(thread.c.id == UUID(claim["provider_thread_record_id"])).values(status="unavailable"))
    world.conn.execute(update(tables["assignment"]).where(tables["assignment"].c.id == assignment["id"]).values(retry_at=None))
    assert world.claim() is None
    assert world.conn.execute(select(func.count()).select_from(thread).where(thread.c.assignment_id == assignment["id"])).scalar_one() == 1


def test_pause_preserves_queue_and_resume_allows_same_assignment(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    paused = world.command("pause_run", run, note="Pause admissions")
    assert paused["status"] == "paused"
    assert world.claim() is None
    assert get(world.conn, "assignment", assignment["id"])["status"] == "pending"
    resumed = world.command("resume_run", paused)
    assert resumed["status"] == "active"
    assert UUID(world.claim()["assignment_id"]) == assignment["id"]
    with pytest.raises(DomainError) as error:
        world.command("pause_run", run)
    assert error.value.code == "revision_conflict"


def test_automation_deferral_survives_unrelated_worker_completion(world):
    run = world.run()
    planner = dict(world.conn.execute(select(tables["automation"]).where(tables["automation"].c.run_id == run["id"],
        tables["automation"].c.name == "planner")).mappings().one())
    deferred_until = datetime.now(timezone.utc) + timedelta(hours=1)
    deferred = world.command("defer_automation", planner, not_before=deferred_until.isoformat())
    worker = world.assignment(run)
    claim = world.claim()
    assert UUID(claim["assignment_id"]) == worker["id"]
    obligation = world.ledger(worker["id"])[0]
    world.service.resolve_obligation(world.conn, world.actor, obligation["id"], models.ObligationResolve(
        expected_revision=1, status="done", resolution={"kind": "completed", "note": "Completed useful work", "evidence": []}))
    world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", claim["execution_id"]), "succeeded")
    assignment = tables["assignment"]
    pending = world.conn.execute(select(assignment).where(assignment.c.automation_id == planner["id"], assignment.c.status == "pending")).mappings().one()
    assert pending["not_before"] == deferred_until
    assert get(world.conn, "automation", planner["id"])["revision"] == deferred["revision"]
    assert world.claim() is None


def test_run_cancellation_settles_after_worker_stop_and_does_not_recreate_automations(world):
    run = world.run()
    claim = world.claim()
    stopping = world.command("cancel_run", run, note="Stop campaign")
    assert stopping["status"] == "stopping"
    assert world.claim() is None
    world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", claim["execution_id"]), "cancelled")
    world.scheduler.tick(world.conn)
    assert get(world.conn, "run", run["id"])["status"] == "cancelled"
    assignment = tables["assignment"]
    assert world.conn.execute(select(func.count()).select_from(assignment).where(assignment.c.run_id == run["id"],
        assignment.c.status.in_(("pending", "running", "stopping")))).scalar_one() == 0


def test_adoption_updates_effective_baseline_and_preserves_initial_phase(world):
    artifact = create(world.conn, "artifact", project_id=world.project["id"], kind="blob",
        content={"sha256": "a" * 64, "size_bytes": 0, "media_type": "application/json"})
    snapshots = [create(world.conn, "roadmap_snapshot", project_id=world.project["id"],
        roadmap_document_id=world.document["id"], source_commit_oid=oid * 40,
        graph_manifest_artifact_id=artifact["id"]) for oid in ("a", "b")]
    run = world.scheduler.run(world.conn, world.actor, models.RunCreate(orchestration="legacy", mission_id=world.mission["id"],
        phase={"kind": "formalization", "roadmap_snapshot_id": snapshots[0]["id"]}, host_ids=[world.host["id"]]))
    adopted = world.command("adopt_roadmap_snapshot", run, snapshot_id=str(snapshots[1]["id"]))
    assert adopted["adopted_roadmap_snapshot_id"] == snapshots[1]["id"]
    assert adopted["phase"]["roadmap_snapshot_id"] == str(snapshots[0]["id"])


def test_drain_and_reopen_restore_operational_work(world):
    run = world.run()
    world.command("complete_mission", world.mission, note="The intended result is complete")
    draining = world.command("drain_run", run)
    completed = world.command("complete_run", draining)
    reopened = world.command("reopen_run", completed, note="A substantive counterexample requires repair")
    assert reopened["status"] == "active"
    assert get(world.conn, "mission", world.mission["id"])["status"] == "open"
    assignment = tables["assignment"]
    pending = list(world.conn.execute(select(assignment).where(assignment.c.run_id == run["id"],
        assignment.c.status == "pending", assignment.c.automation_id.is_not(None))).mappings())
    assert len(pending) == 2


def test_semantic_mission_decision_requires_reason_and_reopening_preserves_history(world):
    with pytest.raises(DomainError) as error:
        world.command("complete_mission", world.mission, note="   ")
    assert error.value.code == "invalid_decision"
    completed = world.command("complete_mission", world.mission,
        note="The mathematical target is fulfilled; see the reviewed source")
    assert completed["closed_at"] is not None
    reopened = world.command("reopen_mission", completed, note="New source evidence changes the target")
    assert reopened["status"] == "open"
    assert reopened["closed_at"] is None and reopened["closure_note"] is None
    revision, reference = tables["record_revision"], tables["object_reference"]
    histories = list(world.conn.execute(select(revision.c.content).select_from(revision.join(reference,
        revision.c.object_id == reference.c.id)).where(reference.c.mission_id == world.mission["id"]).order_by(
            revision.c.object_revision)).scalars())
    assert [item["status"] for item in histories] == ["open", "completed", "open"]
    assert histories[1]["closure_note"] == completed["closure_note"]


def test_persistent_planner_condition_cannot_wait_for_its_own_occurrence(world):
    run = world.run()
    automation = dict(world.conn.execute(select(tables["automation"]).where(tables["automation"].c.run_id == run["id"],
        tables["automation"].c.name == "planner")).mappings().one())
    pending = world.conn.execute(select(tables["assignment"]).where(tables["assignment"].c.automation_id == automation["id"])).mappings().one()
    with pytest.raises(DomainError) as error:
        world.command("defer_automation", automation, start_condition={"version": 1, "expression": {
            "op": "status_in", "target": {"kind": "assignment", "id": str(pending["id"])}, "values": ["completed"]}})
    assert error.value.code == "dependency_cycle"


def test_repeated_commit_discovery_preserves_one_verified_publication(world):
    run = world.run()
    world.disable_automations(run)
    world.assignment(run)
    grant = world.claim()
    payload = {"repository_id": str(world.workspace_repo["id"]), "commit_oid": "a" * 40,
               "recovery_ref": "refs/horizon/recovery/" + "a" * 40,
               "workspace_path": grant["workspace_path"]}

    def send(kind, details):
        return handle_worker_operation(world.conn, world.host_actor, WorkerOperation(
            operation_id=uuid4(), execution_id=grant["execution_id"], epoch=grant["epoch"],
            kind=kind, payload=details, occurred_at=datetime.now(timezone.utc).timestamp()), world.service, world.scheduler)

    discovered = send("publication_discovered", payload)
    verified = send("publication_verified", {**payload, "remote_ref": "refs/heads/horizon/recovery/" + "a" * 40})
    assert verified["publication_id"] == discovered["publication_id"]
    rediscovered = send("publication_discovered", payload)
    assert rediscovered["publication_id"] == verified["publication_id"]
    assert rediscovered["status"] == "verified"
    repeated = send("publication_verified", {**payload, "remote_ref": "refs/heads/horizon/recovery/" + "a" * 40})
    assert repeated["publication_id"] == verified["publication_id"]


def test_semantic_decision_reasons_are_durable_events(world):
    completed = world.command("complete_mission", world.mission, note="The intended theorem is proved")
    world.command("reopen_mission", completed, note="The source now includes a stronger conclusion")
    event, reference = tables["event"], tables["object_reference"]
    changes = list(world.conn.execute(select(event.c.payload).select_from(event.join(reference,
        event.c.subject_id == reference.c.id)).where(reference.c.mission_id == world.mission["id"],
        event.c.kind == "status_changed").order_by(event.c.sequence)).scalars())
    assert [change["note"] for change in changes] == ["The intended theorem is proved", "The source now includes a stronger conclusion"]


def test_lease_loss_does_not_resubmit_until_prior_provider_process_is_reconciled(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    grant = world.claim()
    thread_id = UUID(grant["provider_thread_record_id"])
    world.conn.execute(update(tables["provider_thread"]).where(tables["provider_thread"].c.id == thread_id).values(
        status="available", provider_thread_id="existing-native-context"))
    request = create(world.conn, "provider_request", provider_thread_id=thread_id, execution_id=UUID(grant["execution_id"]),
                     number=1, reason="continuation", status="running")
    world.conn.execute(update(tables["execution"]).where(tables["execution"].c.id == UUID(grant["execution_id"])).values(
        lease_expires_at=func.now() - timedelta(seconds=1)))
    world.scheduler.tick(world.conn)
    assert get(world.conn, "provider_request", request["id"])["status"] == "uncertain"
    world.conn.execute(update(tables["assignment"]).where(tables["assignment"].c.id == assignment["id"]).values(retry_at=None))
    assert world.claim() is None
    handle_worker_operation(world.conn, world.host_actor, WorkerOperation(
        operation_id=uuid4(), execution_id=grant["execution_id"], epoch=grant["epoch"], kind="execution_finished",
        payload={"status": "lost", "reason": "worker_restarted", "provider_thread_id": "existing-native-context"},
        occurred_at=datetime.now(timezone.utc).timestamp()), world.service, world.scheduler)
    assert get(world.conn, "provider_request", request["id"])["status"] == "interrupted"
    resumed = world.claim()
    assert resumed is not None and resumed["provider_thread_record_id"] == grant["provider_thread_record_id"]
    assert resumed["provider_thread_id"] == "existing-native-context"


def test_no_progress_continuations_end_in_owned_reconsideration(world):
    run = world.run(retry_policy={"max_no_progress_requests": 2})
    world.disable_automations(run)
    assignment = world.assignment(run)
    world.assignment(run, functions=["planner"], not_before=datetime.now(timezone.utc) + timedelta(hours=1))
    grant = world.claim()
    for number in (1, 2):
        create(world.conn, "provider_request", provider_thread_id=UUID(grant["provider_thread_record_id"]),
               execution_id=UUID(grant["execution_id"]), number=number, reason="continuation", status="completed")
    assert world.service.progress_stalled(world.conn, assignment["id"], 2)
    result = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", grant["execution_id"]), "yielded")
    assert result["status"] == "failed"
    assert "reconsideration" in result["status_note"]
    assignments, obligations = tables["assignment"], tables["obligation"]
    planner = world.conn.execute(select(assignments).where(assignments.c.run_id == run["id"],
        assignments.c.functions.contains(["planner"]), assignments.c.status == "pending")).mappings().one()
    decisions = list(world.conn.execute(select(obligations.c.description).where(
        obligations.c.assignment_id == planner["id"], obligations.c.status == "open")).scalars())
    assert any(str(assignment["id"]) in description for description in decisions)
