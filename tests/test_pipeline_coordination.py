from datetime import datetime, timedelta, timezone

from sqlalchemy import insert

from archon_horizon.pipeline.coordination import global_health, summary
from archon_horizon.pipeline.records import change, create
from archon_horizon.pipeline.schema import tables
from test_pipeline_service import service_database, world


def test_context_counts_actual_admissions_and_retained_sessions(world):
    run = world.run(max_assignments=1)
    world.disable_automations(run)
    assignment = world.assignment(run)
    assert assignment["number"] > 1
    before = world.service.context(world.conn, world.actor, assignment["id"])["coordination"]
    assert before["admission"]["remaining_new_assignments"] == 1
    world.claim()
    after = summary(world.conn, world.service, run)
    assert after["admission"]["admitted_assignments"] == 1
    assert after["admission"]["remaining_new_assignments"] == 0
    assert after["admission"]["new_assignment_blocker"] == "Run assignment limit reached; existing contexts may still resume"
    assert after["admission"]["retained_assignment_blocker"] is None
    assert {"role": "worker", "status": "running", "count": 1} in after["assignments"]
    assert after["healthy_capacity_pools"][0]["free_slots"] == 1


def test_capacity_accounts_for_other_runs_and_unconfirmed_stops(world):
    other = world.run()
    world.disable_automations(other)
    world.assignment(other)
    grant = world.claim()
    run = world.run()
    change(world.conn, "execution", grant["execution_id"], status="lost", stop_confirmed_at=None)
    observed = summary(world.conn, world.service, run)
    assert observed["healthy_capacity_pools"][0]["occupied"] == 1
    assert observed["healthy_capacity_pools"][0]["free_slots"] == 1
    assert not any(row["status"] == "running" for row in observed["assignments"])
    change(world.conn, "host", world.host["id"], heartbeat_at=datetime.now(timezone.utc) - timedelta(days=1))
    assert summary(world.conn, world.service, run)["healthy_capacity_pools"] == []


def test_shared_resource_recovery_limit_is_not_mistaken_for_free_capacity(world):
    run = world.run()
    world.disable_automations(run)
    world.assignment(run)
    grant = world.claim()
    limit = create(world.conn, "resource_limit", kind="provider_account", slug="shared-provider", max_concurrent=6,
                   failure_count=1)
    world.conn.execute(insert(tables["host_harness_limit"]).values(host_id=world.host["id"],
        harness_id=world.harness["id"], resource_limit_id=limit["id"]))
    create(world.conn, "resource_claim", resource_limit_id=limit["id"], execution_id=grant["execution_id"], units=1)
    observed = summary(world.conn, world.service, run)
    pool = observed["healthy_capacity_pools"][0]
    assert pool["free_slots"] == 1
    assert pool["shared_resources_available"] is False
    assert observed["shared_resources"] == [{"id": limit["id"], "kind": "provider_account", "remaining": 0}]
    assert "credential_ref" not in pool and "provider_home" not in pool


def test_unlimited_admissions_are_still_counted_and_pr_counts_are_run_scoped(world):
    run = world.run()
    world.disable_automations(run)
    world.assignment(run)
    world.claim()
    other = world.run()
    for number, origin, status in [(1, run, "merged"), (2, run, "open"), (3, other, "merged")]:
        create(world.conn, "forge_item", repository_id=world.workspace_repo["id"], remote_number=number,
               kind="pull_request", title="A coherent contribution", status=status, origin_run_id=origin["id"],
               observed_at=datetime.now(timezone.utc))
    observed = summary(world.conn, world.service, run)
    assert observed["admission"]["admitted_assignments"] == 1
    assert observed["admission"]["remaining_new_assignments"] is None
    assert observed["pull_requests"] == {"open": 1, "merged": 1}


def test_global_health_separates_pending_obligations_from_physical_capacity(world):
    run = world.run()
    world.disable_automations(run)
    pending = world.assignment(run)
    health = global_health(world.conn, world.service)
    assert health["pending"]["total"] == 1
    assert health["capacity"]["total_slots"] == 2
    assert health["capacity"]["free_slots"] == 2
    assert health["maintainer_admission"]["one_outstanding_per_automation"] is True
    assert health["queue_policy"]["max_outstanding_per_automation"] == 1
    assert health["maintainer_admission"]["allowed"] is True

    world.claim()
    health = global_health(world.conn, world.service)
    assert health["capacity"]["occupied_slots"] == 1
    assert health["capacity"]["free_slots"] == 1
    assert health["pending"]["total"] == 0
    assert health["active"]["live_by_role"]["worker"] == 1
