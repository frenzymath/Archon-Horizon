import json

import httpx
import pytest

from test_pipeline_api import api, api_database, auth
from archon_horizon.pipeline.records import create

from archon_horizon.pipeline.connectors import ConnectorFailure, ZulipClient


def client_for(handler):
    return ZulipClient("https://zulip.invalid", "worker@example.invalid", "secret",
                       client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_remote_discovery_and_search_are_bounded_and_channel_scoped():
    def handler(request):
        assert request.method == "GET"
        if request.url.path.endswith("/topics"):
            assert request.url.path == "/api/v1/users/me/7/topics"
            return httpx.Response(200, json={"topics": [{"name": "Metric old", "max_id": 4},
                {"name": "Metric new", "max_id": 9}, {"name": "Bundle", "max_id": 8}]})
        narrow = json.loads(request.url.params["narrow"])
        assert narrow == [{"operator": "stream", "operand": 7}, {"operator": "search", "operand": "stream:private"},
                          {"operator": "topic", "operand": "Metric old"}]
        assert request.url.params["num_before"] == "2"
        assert request.url.params["anchor"] == "9"
        return httpx.Response(200, json={"messages": [{"id": 4, "stream_id": 7, "subject": "Metric old",
            "content": "x" * 2000, "sender_id": 2, "timestamp": 1710000000}], "found_oldest": True})
    client = client_for(handler)
    topics = client.topics("7", q="metric", limit=1)
    assert topics == {"items": [{"name": "Metric new", "max_id": 9}], "next_before": 9}
    assert client.topics("7", q="metric", before=9, limit=1)["items"][0]["name"] == "Metric old"
    result = client.search_messages("7", q="stream:private", topic="Metric old", before=9, limit=2)
    assert result["next_before"] is None
    assert len(result["items"][0]["excerpt"]) == 1200
    assert result["items"][0]["excerpt_truncated"]


def test_remote_search_rejects_response_outside_bound_channel():
    client = client_for(lambda request: httpx.Response(200, json={"messages": [
        {"id": 1, "stream_id": 8, "subject": "Other project", "content": "private"}]}))
    with pytest.raises(ConnectorFailure, match="malformed_zulip_search"):
        client.search_messages("7", q="metric")


@pytest.mark.parametrize("change", ["new", "old_edit", "deleted"])
def test_delivery_checks_entire_participation_scope_beyond_last_100(change):
    messages = [{"id": number, "content": f"Message {number}"} for number in range(41, 161)]
    expected = {str(row["id"]): row["content"] for row in messages}
    if change == "new":
        messages.append({"id": 161, "content": "New answer"})
    elif change == "old_edit":
        messages[0]["content"] = "Earlier correction"
    else:
        messages.pop(0)
    def handler(request):
        assert request.method == "GET", "must reject before sending"
        assert int(request.url.params["anchor"]) == 40
        return httpx.Response(200, json={"messages": messages, "found_newest": True})
    with pytest.raises(ConnectorFailure, match="reply_requires_updated_read_receipts"):
        client_for(handler).post("7", "Metric", "Answer", "op", expected_message_ids=set(expected),
            expected_message_bodies=expected, read_start_remote_id=41)


def test_scoped_delivery_allows_acknowledged_tombstone_and_reconciles_existing_marker():
    sent = []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"messages": [{"id": 50, "content": "Answer\n<!-- horizon-operation:op -->",
                "sender_email": "worker@example.invalid"}], "found_newest": True})
        sent.append(request)
        return httpx.Response(200, json={"id": 51})
    result = client_for(handler).post("7", "Metric", "Answer", "op", reconcile_only=True,
        expected_message_ids=set(), expected_message_bodies={}, read_start_remote_id=41)
    assert result["id"] == 50
    assert sent == []


def test_discovery_api_uses_existing_project_binding_and_returns_canonical_links(api, monkeypatch):
    client, database, world, _, token, _ = api
    with database.transaction() as conn:
        from datetime import datetime, timezone
        integration = create(conn, "integration", kind="zulip", endpoint="https://zulip.invalid", credential_ref="secret:zulip")
        discussion = create(conn, "discussion", project_id=world.project["id"], integration_id=integration["id"],
            channel_remote_id="7", topic="Existing", observed_at=datetime.now(timezone.utc))
    requests = []
    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/topics"):
            return httpx.Response(200, json={"topics": [{"name": "Earlier decision", "max_id": 99}]})
        return httpx.Response(200, json={"messages": [{"id": 99, "stream_id": 7,
            "subject": "Earlier decision", "content": "Use generic scalar fields"}], "found_oldest": True})
    monkeypatch.setattr("archon_horizon.pipeline.connectors.ConnectorManager.remote_client",
                        lambda self, integration: client_for(handler))
    response = client.get(f"/api/v3/discussions/{discussion['id']}/topics", headers=auth(token))
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["url"].endswith("/topic/Earlier%20decision")
    response = client.get(f"/api/v3/discussions/{discussion['id']}/search", headers=auth(token), params={"q": "scalar"})
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["url"].endswith("/near/99")
    response = client.get(f"/api/v3/discussions/{discussion['id']}/search", headers=auth(token), params={"q": "scalar", "channel": "8"})
    assert response.status_code == 422
    assert len(requests) == 2


def test_discovery_denies_ungranted_project_before_remote_request(api, monkeypatch):
    client, database, world, _, _, _ = api
    from datetime import datetime, timezone
    from archon_horizon.pipeline.auth import issue_credential
    with database.transaction() as conn:
        outsider = create(conn, "principal", kind="human", display_name="Outside reader", username="outside-reader")
        _, token = issue_credential(conn, outsider["id"], "api_key", "Outside project")
        integration = create(conn, "integration", kind="zulip", endpoint="https://zulip.invalid", credential_ref="secret:zulip")
        discussion = create(conn, "discussion", project_id=world.project["id"], integration_id=integration["id"],
            channel_remote_id="7", topic="Existing", observed_at=datetime.now(timezone.utc))
    def forbidden(*args, **kwargs):
        raise AssertionError("No remote request before project authorization")
    monkeypatch.setattr("archon_horizon.pipeline.connectors.ConnectorManager.remote_client", forbidden)
    for route in ("topics", "search?q=scalar"):
        response = client.get(f"/api/v3/discussions/{discussion['id']}/{route}", headers=auth(token))
        assert response.status_code == 403, response.text
