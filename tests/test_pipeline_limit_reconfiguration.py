from datetime import datetime, timezone
from uuid import UUID

import pytest
from sqlalchemy import func, select

from archon_horizon.pipeline.projects.catalog import CatalogUpdate, configure_host_harness
from archon_horizon.pipeline.persistence.records import change, create, get, next_number
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_service import make_world, service_database, world  # noqa: F401


def launch(world):
    run = world.run()
    world.disable_automations(run)
    world.assignment(run)
    return run, world.claim()


def configure(world, limits):
    return configure_host_harness(world.conn, world.actor, world.host["id"], world.harness["id"],
        CatalogUpdate(expected_revision=get(world.conn, "host", world.host["id"])["revision"],
            changes={"resource_limit_ids": [limit["id"] for limit in limits]}))


def claims(world, limit, *, active=True):
    table = tables["resource_claim"]
    query = select(table).where(table.c.resource_limit_id == limit["id"])
    if active:
        query = query.where(table.c.released_at.is_(None))
    return [dict(row) for row in world.conn.execute(query.order_by(table.c.id)).mappings()]


def child(world, grant, parent, status):
    thread = get(world.conn, "provider_thread", grant["provider_thread_record_id"])
    context = create(world.conn, "provider_thread", assignment_id=thread["assignment_id"], kind="child",
        number=next_number(world.conn, "provider_thread", "assignment_id", thread["assignment_id"]),
        parent_request_id=parent["id"], workspace_id=thread["workspace_id"],
        harness_revision_id=thread["harness_revision_id"], skill_bundle_artifact_id=thread["skill_bundle_artifact_id"],
        provider_state_ref="child-" + status, applied_model_options={}, status="available")
    return create(world.conn, "provider_request", provider_thread_id=context["id"],
        execution_id=UUID(grant["execution_id"]), number=1, reason="continuation", status=status)


def test_new_limits_account_for_parents_and_native_reservations_without_stopping_them(world):
    run, grant = launch(world)
    parent = create(world.conn, "provider_request", provider_thread_id=UUID(grant["provider_thread_record_id"]),
        execution_id=UUID(grant["execution_id"]), number=1, reason="assignment", status="running")
    active = [child(world, grant, parent, status) for status in ("pending", "submitted", "running", "uncertain")]
    for status in ("completed", "failed", "interrupted"):
        child(world, grant, parent, status)
    provider = create(world.conn, "resource_limit", kind="provider_account", slug="new-provider", max_concurrent=3)
    build = create(world.conn, "resource_limit", kind="build_pool", slug="new-build", max_concurrent=2)
    configure(world, [provider, build])
    assert {row["provider_request_id"] for row in claims(world, provider)} == {None, *(row["id"] for row in active)}
    assert sum(row["units"] for row in claims(world, provider)) == 5
    assert len(claims(world, build)) == 1 and claims(world, build)[0]["provider_request_id"] is None
    assert get(world.conn, "execution", grant["execution_id"])["status"] == "running"
    assert all(get(world.conn, "provider_request", row["id"])["status"] == row["status"] for row in active)
    world.assignment(run)
    assert world.claim() is None


def test_reconfiguring_detaching_and_reattaching_limits_preserves_existing_claims(world):
    _, grant = launch(world)
    provider = create(world.conn, "resource_limit", kind="provider_account", slug="shared", max_concurrent=8)
    configure(world, [provider])
    original = claims(world, provider)
    configure(world, [provider, provider])
    assert claims(world, provider) == original
    configure(world, [])
    assert claims(world, provider) == original
    configure(world, [provider])
    assert claims(world, provider) == original
    world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", grant["execution_id"]), "failed")
    assert claims(world, provider) == []
    history = claims(world, provider, active=False)
    assert len(history) == 1 and history[0]["released_at"] is not None
    configure(world, [])
    configure(world, [provider])
    assert claims(world, provider, active=False) == history


@pytest.mark.parametrize("status", ["starting", "running", "stopping", "lost", "failed", "cancelled", "succeeded"])
def test_unconfirmed_execution_preserves_physical_occupancy_when_limit_is_added(world, status):
    _, grant = launch(world)
    change(world.conn, "execution", grant["execution_id"], status=status, stop_confirmed_at=None)
    provider = create(world.conn, "resource_limit", kind="provider_account", slug="new-limit", max_concurrent=8)
    configure(world, [provider])
    assert [(row["execution_id"], row["provider_request_id"]) for row in claims(world, provider)] == [
        (UUID(grant["execution_id"]), None)]


def test_confirmed_history_and_stale_child_rows_do_not_regain_claims(world):
    _, grant = launch(world)
    parent = create(world.conn, "provider_request", provider_thread_id=UUID(grant["provider_thread_record_id"]),
        execution_id=UUID(grant["execution_id"]), number=1, reason="assignment", status="completed")
    child(world, grant, parent, "running")
    change(world.conn, "execution", grant["execution_id"], status="succeeded", stop_confirmed_at=datetime.now(timezone.utc))
    provider = create(world.conn, "resource_limit", kind="provider_account", slug="after-stop", max_concurrent=8)
    configure(world, [provider])
    assert claims(world, provider, active=False) == []


def test_shared_limit_attached_to_two_hosts_counts_only_each_matching_harness(world, tmp_path):
    run, first = launch(world)
    other = make_world(world.conn, tmp_path / "other-host")
    _, second = launch(other)
    provider = create(world.conn, "resource_limit", kind="provider_account", slug="both-hosts", max_concurrent=2)
    configure(world, [provider])
    assert {row["execution_id"] for row in claims(world, provider)} == {UUID(first["execution_id"])}
    configure(other, [provider])
    assert {row["execution_id"] for row in claims(world, provider)} == {
        UUID(first["execution_id"]), UUID(second["execution_id"])}
    world.assignment(run)
    assert world.claim() is None
    assert world.conn.execute(select(func.count()).select_from(tables["resource_claim"])
        .where(tables["resource_claim"].c.resource_limit_id == provider["id"])).scalar_one() == 2
