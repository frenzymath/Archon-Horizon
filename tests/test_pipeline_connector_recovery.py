from datetime import datetime, timezone
import json

import httpx
import pytest
from sqlalchemy import select, update

from archon_horizon.pipeline.auth import Actor
from archon_horizon.pipeline.integrations.communications import queue_reply
from archon_horizon.pipeline.integrations.connectors import ConnectorFailure, ZulipClient
from archon_horizon.pipeline.persistence.records import create, get, transaction_lock
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_connectors import connector_world, manager


def test_idle_poll_refreshes_queue_then_verifies_response_after_local_deadline():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.params["dont_block"] == "false":
            assert request.extensions["timeout"]["read"] == 10
            raise httpx.ReadTimeout("idle long poll", request=request)
        return httpx.Response(200, json={"events": [{"id": 7, "type": "heartbeat"}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = ZulipClient("https://zulip.invalid", "worker@example.invalid", "secret", client=transport)
        assert client.events("queue", "6") == [{"id": 7, "type": "heartbeat"}]
    assert [request.url.params["dont_block"] for request in requests] == ["false", "true"]
    assert all(request.url.params["last_event_id"] == "6" for request in requests)


def test_idle_poll_does_not_hide_real_connection_failure():
    def handler(request):
        raise httpx.ReadTimeout("server unreachable", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = ZulipClient("https://zulip.invalid", "worker@example.invalid", "secret", client=transport)
        with pytest.raises(ConnectorFailure, match="transport_unavailable"):
            client.events("queue", "6")


def test_shutdown_does_not_start_another_remote_request():
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("idle long poll", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = ZulipClient("https://zulip.invalid", "worker@example.invalid", "secret", client=transport)
        client.should_stop = lambda: bool(requests)
        with pytest.raises(ConnectorFailure, match="connector_stopping"):
            client.events("queue", "6")
    assert len(requests) == 1


@pytest.mark.parametrize("response", [{"messages": []}, {"messages": [{"id": "wrong"}]},
                                     {"messages": [], "history_limited": True}])
def test_incomplete_history_cannot_be_treated_as_empty_topic(response):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))) as transport:
        client = ZulipClient("https://zulip.invalid", "worker@example.invalid", "secret", client=transport)
        with pytest.raises(ConnectorFailure):
            client.topic_messages("1", "Proof")


def test_new_discussion_backfills_with_existing_current_event_queue(connector_world):
    world = connector_world
    history = []

    def handler(request):
        if request.url.path.endswith("/register"):
            return httpx.Response(200, json={"queue_id": "queue", "last_event_id": -1})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"events": []})
        topic = json.loads(request.url.params["narrow"])[1]["operand"]
        history.append(topic)
        messages = [{"id": 2, "sender_id": 9, "timestamp": 1000, "content": "Prior discussion",
                     "stream_id": 1, "subject": topic}] if topic == "New topic" else []
        return httpx.Response(200, json={"messages": messages, "found_newest": True})

    connector = manager(world, handler)
    connector.sync_zulip(world.zulip)
    with world.database.transaction() as conn:
        transaction_lock(conn)
        topic = create(conn, "discussion", project_id=world.project["id"], integration_id=world.zulip["id"],
                       channel_remote_id="1", topic="New topic", sync_status="reconciling",
                       observed_at=datetime.now(timezone.utc))
    history.clear()
    connector.sync_zulip(world.zulip)
    assert history == ["New topic"]
    with world.database.transaction() as conn:
        assert get(conn, "discussion", topic["id"])["sync_status"] == "current"
        assert conn.execute(select(tables["message"].c.body).where(
            tables["message"].c.discussion_id == topic["id"])).scalar_one() == "Prior discussion"


def test_inaccessible_repository_does_not_block_other_repository_reconciliation(connector_world):
    world = connector_world
    with world.database.transaction() as conn:
        healthy = create(conn, "repository", project_id=world.project["id"], integration_id=world.forge["id"],
                         slug="library", remote_id="2", remote_path="owner/library", default_branch="main", purpose="library")

    def handler(request):
        if request.url.path.startswith("/api/v1/repos/owner/repo"):
            return httpx.Response(403)
        if request.url.path.endswith("/library"):
            return httpx.Response(200, json={"has_issues": True, "has_pull_requests": True})
        if request.url.path.endswith("/issues"):
            return httpx.Response(200, json=[{"number": 1, "title": "Review needed", "state": "open"}])
        return httpx.Response(200, json=[])

    assert manager(world, handler).sync_all("forge") == {str(world.forge["id"]): "http_403"}
    with world.database.transaction() as conn:
        cursors = {row["consumer"]: row["status"] for row in conn.execute(select(tables["connector_cursor"])).mappings()}
        assert cursors == {"forge": "unavailable", "forge:" + str(world.repository["id"]): "unavailable",
                           "forge:" + str(healthy["id"]): "current"}
        assert conn.execute(select(tables["forge_item"].c.repository_id)).scalar_one() == healthy["id"]


def test_repeated_queue_expiry_is_reconciling_not_integration_outage(connector_world):
    world = connector_world
    registrations = []

    def handler(request):
        if request.url.path.endswith("/register"):
            registrations.append(request)
            return httpx.Response(200, json={"queue_id": str(len(registrations)), "last_event_id": -1})
        if request.url.path.endswith("/events"):
            return httpx.Response(400, json={"code": "BAD_EVENT_QUEUE_ID"})
        return httpx.Response(200, json={"messages": [], "found_newest": True})

    connector = manager(world, handler)
    assert connector.sync_all("zulip") == {str(world.zulip["id"]): "zulip_queue_replaced"}
    assert len(registrations) == 2
    with world.database.transaction() as conn:
        assert get(conn, "discussion", world.discussion["id"])["sync_status"] == "reconciling"
        assert conn.execute(select(tables["connector_cursor"].c.status)).scalar_one() == "reconciling"


def test_interrupted_event_batch_is_committed_but_not_acknowledged_until_replayed(connector_world):
    world = connector_world
    messages = [{"id": number, "sender_id": 9, "timestamp": 1000, "content": f"Message {number}",
                 "stream_id": 1, "subject": "Proof"} for number in range(1, 151)]
    stopped = False
    first_history = True

    def handler(request):
        nonlocal first_history
        if request.url.path.endswith("/register"):
            return httpx.Response(200, json={"queue_id": "queue", "last_event_id": -1})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"events": [
                {"id": message["id"], "type": "message", "message": message} for message in messages]})
        history = [] if first_history else messages
        first_history = False
        return httpx.Response(200, json={"messages": history, "found_newest": True})

    connector = manager(world, handler)
    connector.should_stop = lambda: stopped
    original_upsert = connector._upsert_message

    def upsert(conn, discussion, message):
        nonlocal stopped
        result = original_upsert(conn, discussion, message)
        if message["id"] == 100:
            stopped = True
        return result

    connector._upsert_message = upsert
    assert connector.sync_all("zulip") == {str(world.zulip["id"]): "connector_stopping"}
    with world.database.transaction() as conn:
        cursor = conn.execute(select(tables["connector_cursor"])).mappings().one()
        assert cursor["last_event_remote_id"] == "-1"
        assert cursor["status"] == "reconciling"
        assert len(list(conn.execute(select(tables["message"].c.id)))) == 100
    stopped = False
    connector._upsert_message = original_upsert
    assert connector.sync_all("zulip") == {str(world.zulip["id"]): "current"}
    with world.database.transaction() as conn:
        cursor = conn.execute(select(tables["connector_cursor"])).mappings().one()
        assert cursor["last_event_remote_id"] == "150"
        assert len(list(conn.execute(select(tables["message"].c.id)))) == 150


def test_topic_move_and_delete_update_only_existing_and_destination_discussions(connector_world):
    world = connector_world
    with world.database.transaction() as conn:
        destination = create(conn, "discussion", project_id=world.project["id"], integration_id=world.zulip["id"],
                             channel_remote_id="1", topic="Other", observed_at=datetime.now(timezone.utc))
    message = {"id": 1, "sender_id": 9, "timestamp": 1000, "content": "Moved proof", "stream_id": 1, "subject": "Proof"}
    events = []

    def handler(request):
        if request.url.path.endswith("/register"):
            return httpx.Response(200, json={"queue_id": "queue", "last_event_id": -1})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"events": events})
        if request.url.path.endswith("/messages/1"):
            return httpx.Response(200, json={"message": message})
        topic = json.loads(request.url.params["narrow"])[1]["operand"]
        return httpx.Response(200, json={"messages": [message] if topic == "Proof" else [], "found_newest": True})

    connector = manager(world, handler)
    connector.sync_zulip(world.zulip)
    message["subject"] = "Other"
    events = [{"id": 10, "type": "update_message", "message_id": 1},
              {"id": 11, "type": "message", "message": {**message, "id": 2, "subject": "Unsubscribed"}}]
    connector.sync_zulip(world.zulip)
    with world.database.transaction() as conn:
        messages = {row["discussion_id"]: row for row in conn.execute(select(tables["message"])).mappings()}
        assert len(messages) == 2
        assert messages[world.discussion["id"]]["deleted_at"] is not None
        assert messages[destination["id"]]["deleted_at"] is None
    events = [{"id": 12, "type": "delete_message", "message_ids": [1]}]
    connector.sync_zulip(world.zulip)
    with world.database.transaction() as conn:
        assert all(value is not None for value in conn.execute(select(tables["message"].c.deleted_at)).scalars())


@pytest.mark.parametrize("initial_status", ["pending", "uncertain"])
def test_discussion_reconciliation_defers_without_retry_exhaustion_or_losing_uncertainty(connector_world, initial_status):
    world = connector_world
    with world.database.transaction() as conn:
        transaction_lock(conn)
        actor = Actor(world.actor["id"], "human", {"username": "maintainer"}, "api_key")
        conn.execute(update(tables["discussion"]).where(tables["discussion"].c.id == world.discussion["id"])
                     .values(sync_status="reconciling"))
        operation = queue_reply(conn, actor, world.service, discussion_id=world.discussion["id"],
                                assignment_id=None, body="Durable handoff", read_revisions=[], urgent=False, key="deferred")
        conn.execute(update(tables["outbox_operation"]).where(tables["outbox_operation"].c.id == operation["id"])
                     .values(status=initial_status, retry_count=20))

    def no_network(request):
        raise AssertionError("must not deliver before reconciliation")

    connector = manager(world, no_network)
    for attempt in range(1, 4):
        assert connector.dispatch_one() == initial_status
        with world.database.transaction() as conn:
            current = get(conn, "outbox_operation", operation["id"])
            assert current["retry_count"] == 20
            assert current["failure"]["message"] == "discussion_not_current"
            assert current["failure"]["deferred_attempts"] == attempt
            assert (current["retry_at"] - current["updated_at"]).total_seconds() >= 4
            conn.execute(update(tables["outbox_operation"]).where(tables["outbox_operation"].c.id == operation["id"])
                         .values(retry_at=None))


@pytest.mark.parametrize("read_bodies", [{}, {"1": "Previous version"}])
def test_delivery_rejects_new_or_edited_unread_messages(read_bodies):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"messages": [{"id": 1, "content": "New version"}], "found_newest": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = ZulipClient("https://zulip.invalid", "worker@example.invalid", "secret", client=transport)
        with pytest.raises(ConnectorFailure, match="reply_requires_updated_read_receipts"):
            client.post("1", "Proof", "Reply", "operation", expected_message_ids=set(read_bodies),
                        expected_message_bodies=read_bodies)


def test_whole_topic_rename_preserves_discussion_and_message_identity(connector_world):
    world = connector_world
    message = {"id": 1, "sender_id": 9, "timestamp": 1000, "content": "Definition decision",
               "stream_id": 1, "subject": "Proof"}
    events = []
    def handler(request):
        if request.url.path.endswith("/register"):
            return httpx.Response(200, json={"queue_id": "queue", "last_event_id": -1})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"events": events})
        if request.url.path.endswith("/messages/1"):
            return httpx.Response(200, json={"message": message})
        return httpx.Response(200, json={"messages": [message], "found_newest": True})
    connector = manager(world, handler)
    connector.sync_zulip(world.zulip)
    with world.database.transaction() as conn:
        original_id = conn.execute(select(tables["message"].c.id)).scalar_one()
    message["subject"] = "Resolved definition"
    events = [{"id": 10, "type": "update_message", "message_ids": [1], "stream_id": 1,
               "orig_subject": "Proof", "subject": "Resolved definition", "propagate_mode": "change_all"}]
    connector.sync_zulip(world.zulip)
    with world.database.transaction() as conn:
        topic = get(conn, "discussion", world.discussion["id"])
        assert topic["topic"] == "Resolved definition"
        row = get(conn, "message", original_id)
        assert row["deleted_at"] is None
        assert row["discussion_id"] == topic["id"]
        assert len(list(conn.execute(select(tables["discussion"].c.id)))) == 1
    connector.sync_zulip(world.zulip)
    with world.database.transaction() as conn:
        assert get(conn, "message", original_id)["deleted_at"] is None
