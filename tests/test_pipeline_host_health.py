from uuid import uuid4

import pytest
from sqlalchemy import func, select

from archon_horizon.pipeline.records import get
from archon_horizon.pipeline.schema import tables
from test_pipeline_api import api, api_database, auth


def test_host_health_updates_liveness_without_admission_or_event_churn(api):
    client, database, world, _, _, token = api
    route = f"/api/v3/worker/hosts/{world.host['id']}/heartbeat"
    def counts(conn):
        return {kind: conn.execute(select(func.count()).select_from(tables[kind])).scalar_one()
                for kind in ("execution", "assignment", "event", "api_request")}
    with database.transaction() as conn:
        before = get(conn, "host", world.host["id"])
        previous_counts = counts(conn)
    for health in ({"status": "storage_pressure", "free_bytes": 5, "required_free_bytes": 100},
                   {"status": "ready", "free_bytes": 500, "required_free_bytes": 100},
                   {"status": "ready", "free_bytes": 500, "required_free_bytes": 100,
                    "capabilities": {"workspace_preparation": 1}}):
        response = client.post(route, json={"health": health}, headers=auth(token))
        assert response.status_code == 200, response.text
        assert response.json()["health"] == health
        with database.transaction() as conn:
            current = get(conn, "host", world.host["id"])
            assert current["health"] == health and current["heartbeat_at"] > before["heartbeat_at"]
            assert current["mode"] == before["mode"]
            assert counts(conn) == previous_counts


def test_host_health_rejects_other_principals_and_cross_host_ids(api):
    client, _, world, _, operator_token, host_token = api
    body = {"health": {"status": "ready", "free_bytes": 500, "required_free_bytes": 100}}
    route = f"/api/v3/worker/hosts/{world.host['id']}/heartbeat"
    assert client.post(route, json=body).status_code == 401
    assert client.post(route, json=body, headers=auth(operator_token)).status_code == 403
    assert client.post(f"/api/v3/worker/hosts/{uuid4()}/heartbeat", json=body, headers=auth(host_token)).status_code == 403


@pytest.mark.parametrize("update", [{"status": "unknown"}, {"free_bytes": -1}, {"required_free_bytes": True},
    {"free_bytes": 2**64}, {"free_bytes": "100"}, {"note": "unbounded data"},
    {"capabilities": {"workspace_preparation": True}}, {"capabilities": {"workspace_preparation": 2}},
    {"capabilities": {"unknown": 1}}])
def test_host_health_diagnostics_are_bounded_and_typed(api, update):
    client, _, world, _, _, token = api
    health = {"status": "ready", "free_bytes": 500, "required_free_bytes": 100, **update}
    response = client.post(f"/api/v3/worker/hosts/{world.host['id']}/heartbeat", json={"health": health}, headers=auth(token))
    assert response.status_code == 422, response.text
