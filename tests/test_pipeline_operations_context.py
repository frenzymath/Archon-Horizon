import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from archon_horizon.pipeline.instructions.prompts import OPERATIONS_CONTEXT_BYTES
from archon_horizon.pipeline.persistence.records import change, create, transaction_lock
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_api import api, api_database, auth
from test_pipeline_review_assessments import setup
from test_pipeline_reviewer_invocations import review
from test_pipeline_service import make_world, service_database, world


@pytest.mark.parametrize("role", ["worker", "maintainer"])
def test_operations_view_is_project_scoped_and_has_authorized_drilldowns(api, tmp_path, role):
    client, database, world, run, _, _ = api
    with database.transaction() as conn:
        transaction_lock(conn)
        world.conn = conn
        world.disable_automations(run)
        owner = world.assignment(run, role=role)
        grant = world.claim()
        assert grant["assignment_id"] == str(owner["id"])
        pending = world.assignment(run, not_before=datetime.now(timezone.utc) + timedelta(hours=1))
        event = create(conn, "activity", assignment_id=owner["id"], execution_id=grant["execution_id"],
            kind="tool_use", occurred_at=datetime.now(timezone.utc),
            summary="Inspected the current contract. Authorization: Bearer activity-private-token")
        change(conn, "execution", grant["execution_id"],
               failure={"code": "transport", "message": "Bearer failure-private-token", "token": "private-metadata"})
        change(conn, "host", world.host["id"], health={"status": "ready", "free_bytes": 1000000000,
               "required_free_bytes": 0, "private_detail": "host-private-secret"})
        item = create(conn, "forge_item", repository_id=world.document["source_repository_id"],
            remote_number=1, kind="pull_request", origin_run_id=run["id"], review_phase="preprocessing",
            target_branch="main", title="Contract awaiting review", status="open", head_commit_oid="a" * 40,
            observed_at=datetime.now(timezone.utc))
        foreign = make_world(conn, tmp_path / "foreign")
        foreign_run = foreign.run()
        foreign_owner = foreign.assignment(foreign_run, instructions="FOREIGN PRIVATE TASK")

    headers = auth(grant["execution_token"])
    url = f"/api/v3/assignments/{owner['id']}/context?view=operations"
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    context = response.json()
    assert context["view"] == "operations" and context["assignment"]["profile"] == role
    assert len((json.dumps(context, indent=2) + "\n").encode()) <= OPERATIONS_CONTEXT_BYTES
    owners = {row["id"]: row for row in context["assignments"]}
    assert owners[str(pending["id"])]["waiting_reason"].startswith("Deferred until")
    recent = owners[str(owner["id"])]["last_activity"]
    assert recent["id"] == str(event["id"]) and recent["summary"].startswith("Inspected the current contract")
    assert "[redacted]" in recent["summary"]
    assert context["hosts"][0]["health"]["free_bytes"] == 1000000000
    assert context["review_readiness"][0]["id"] == str(item["id"])
    for forbidden in ("activity-private-token", "failure-private-token", "private-metadata", "host-private-secret",
                      "FOREIGN PRIVATE TASK", str(foreign_run["id"]), str(foreign_owner["id"]), str(foreign.host["id"])):
        assert forbidden not in response.text
    for path in (recent["detail_url"], owners[str(owner["id"])]["activity_url"],
                 context["collections"]["assignments"]["pending_url"],
                 context["collections"]["review_readiness"]["list_url"]):
        linked = client.get(path, headers=headers)
        assert linked.status_code == 200, linked.text
    assert client.get(f"/api/v3/assignments/{foreign_owner['id']}/context?view=operations", headers=headers).status_code == 403
    assert client.get("/api/v3/resources", headers=headers).status_code == 403
    assert client.get(f"/api/v3/records/host/{world.host['id']}", headers=headers).status_code == 403
    for view in ("brief", "full"):
        ordinary = client.get(f"/api/v3/assignments/{owner['id']}/context?view={view}", headers=headers)
        assert ordinary.status_code == 200, ordinary.text
        assert str(foreign_run["id"]) not in ordinary.text
        global_state = ordinary.json()["coordination"]["global"]
        assert "by_run" not in global_state["pending"]
        assert "live_by_run" not in global_state["active"]
    with database.transaction() as conn:
        change(conn, "execution", grant["execution_id"], lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert client.get(url, headers=headers).status_code == 409


def test_operations_context_is_bounded_and_states_job_window_limit(world):
    run = world.run()
    world.disable_automations(run)
    owner = world.assignment(run)
    grant = world.claim()
    create(world.conn, "activity", assignment_id=owner["id"], execution_id=grant["execution_id"],
        kind="progress", occurred_at=datetime.now(timezone.utc), summary="\u03b1" * 10000)
    for _ in range(15):
        world.assignment(run, instructions="\u03b1" * 10000)
    workspace = world.conn.execute(select(tables["workspace"]).where(
        tables["workspace"].c.project_id == world.project["id"]).limit(1)).mappings().one()
    for _ in range(10):
        create(world.conn, "milestone_job", project_id=world.project["id"], host_id=world.host["id"],
            workspace_id=workspace["id"], principal_id=world.actor.id, request={"token": "not-in-view"})
    context = world.service.context(world.conn, world.actor, owner["id"], view="operations")
    rendered = json.dumps(context, default=str, indent=2) + "\n"
    assert len(rendered.encode()) <= OPERATIONS_CONTEXT_BYTES
    assert context["collections"]["assignments"]["truncated"]
    jobs = context["collections"]["milestone_jobs"]
    assert jobs["total"] == 10 and jobs["included"] <= 8 and jobs["truncated"]
    assert "No paginated job list is available" in jobs["selection_note"]
    assert "detail_url" not in jobs and "list_url" not in jobs
    assert jobs["detail_url_template"] == "/api/v3/milestones/verifications/{id}"
    assert "not-in-view" not in rendered


@pytest.mark.parametrize("mode", ["native", "assignment"])
def test_operations_context_links_current_reviewer_ownership(world, review, mode):
    from archon_horizon.pipeline.review.invocations import prepare, prepare_assignment
    setup(world, review)
    if mode == "assignment":
        change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], invocation="assignment")
        prepared = prepare_assignment(world.conn, review["actor"], review["data"], world.service)
        owner_id, kind = prepared["assignment"]["id"], "assignment"
    else:
        prepared = prepare(world.conn, review["actor"], review["data"], world.service)
        owner_id, kind = prepared["provider_request_id"], "provider_request"
    context = world.service.context(world.conn, review["actor"], review["assignment"]["id"], view="operations")
    item = context["review_readiness"][0]
    assert item["id"] == review["item"]["id"]
    assert item["reviewer_owner_count"] == 1 and not item["reviewer_owners_truncated"]
    assert item["reviewer_owners"] == [{
        "reviewer_descriptor_id": str(review["descriptor"]["id"]), "dimensions": ["source-review"],
        "owner_id": str(owner_id), "owner_kind": kind, "owner_status": "pending",
        "detail_url": f"/api/v3/records/{kind}/{owner_id}",
    }]
