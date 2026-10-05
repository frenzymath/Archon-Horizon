from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import insert, select, update

from archon_horizon.pipeline.auth import Actor, authenticate
from archon_horizon.pipeline.communications import (
    DiscussionRegistration, DiscussionSubjectLink, link_subject, register_discussion,
    queue_reply, read_messages, resolve_mentions, route_message, seed_assignment_subscriptions, subscribe,
)
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.models import SubscriptionCreate
from archon_horizon.pipeline.records import create, emit, get, object_ref
from archon_horizon.pipeline.records import change
from archon_horizon.pipeline.readmodels import discussion_messages
from archon_horizon.pipeline.schema import tables
from test_pipeline_service import service_database, world


def discussion(world, topic="Proof"):
    integration = create(world.conn, "integration", kind="zulip", endpoint="https://zulip.invalid", credential_ref="secret:zulip")
    return register_discussion(world.conn, world.actor, DiscussionRegistration(project_id=world.project["id"],
        integration_id=integration["id"], channel_remote_id="7", topic=topic))


def message(world, topic, body):
    value = create(world.conn, "message", discussion_id=topic["id"], remote_id="1", remote_author_id="author",
                   body=body, posted_at=datetime.now(timezone.utc))
    event = emit(world.conn, None, world.project["id"], "message", value, ["body"])
    return value, event


def assigned(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    return run, assignment


def test_registration_starts_unverified_and_workers_only_fork_bound_channel(world):
    topic = discussion(world)
    assert topic["sync_status"] == "reconciling"
    run, assignment = assigned(world)
    grant = world.claim()
    worker = authenticate(world.conn, grant["execution_token"])
    fork = register_discussion(world.conn, worker, DiscussionRegistration(project_id=world.project["id"],
        source_discussion_id=topic["id"], topic="Hypotheses"))
    assert fork["channel_remote_id"] == topic["channel_remote_id"]
    assert fork["integration_id"] == topic["integration_id"]
    with pytest.raises(DomainError, match="administrator"):
        register_discussion(world.conn, worker, DiscussionRegistration(project_id=world.project["id"],
            integration_id=topic["integration_id"], channel_remote_id="8", topic="Arbitrary channel"))
    other = create(world.conn, "project", slug="other-project", title="Other")
    with pytest.raises(DomainError, match="another project"):
        register_discussion(world.conn, world.actor, DiscussionRegistration(project_id=other["id"],
            integration_id=topic["integration_id"], channel_remote_id="7", topic="Private topic"))


def test_reconciling_discussion_accepts_durable_reply_but_preserves_read_requirements(world):
    topic = discussion(world)
    _, assignment = assigned(world)
    reply = queue_reply(world.conn, world.actor, world.service, discussion_id=topic["id"],
                        assignment_id=assignment["id"], body="Pending handoff", read_revisions=[],
                        urgent=False, key="empty-topic")
    assert reply["status"] == "pending"
    assert reply["payload"]["require_read_receipts"] is True
    value, _ = message(world, topic, "Existing discussion")
    with pytest.raises(DomainError, match="Read the relevant discussion"):
        queue_reply(world.conn, world.actor, world.service, discussion_id=topic["id"],
                    assignment_id=assignment["id"], body="Unread reply", read_revisions=[],
                    urgent=False, key="unread-topic")
    receipt = {"id": str(value["id"]), "revision": value["revision"]}
    read_messages(world.conn, world.actor, assignment["id"], [receipt], world.service)
    reply = queue_reply(world.conn, world.actor, world.service, discussion_id=topic["id"],
                        assignment_id=assignment["id"], body="Read handoff", read_revisions=[receipt],
                        urgent=False, key="read-topic")
    assert reply["status"] == "pending"
    assert reply["payload"]["read_messages"] == [receipt]


def test_assignment_defaults_preserve_explicit_mute_and_scope_nodes(world):
    run, assignment = assigned(world)
    node = create(world.conn, "node", project_id=world.project["id"], number=12, title="Theorem",
                  source_repository_id=world.workspace_repo["id"], source_path="Theorem.lean", source_commit_oid="a" * 40)
    world.conn.execute(insert(tables["mission_node"]).values(mission_id=world.mission["id"], node_id=node["id"]))
    subscribe(world.conn, world.actor, SubscriptionCreate(assignment_id=assignment["id"],
        subject={"kind": "mission", "id": world.mission["id"]}, mode="muted"), world.service)
    seed_assignment_subscriptions(world.conn, assignment)
    seed_assignment_subscriptions(world.conn, assignment)
    rows = list(world.conn.execute(select(tables["subscription"]).where(
        tables["subscription"].c.assignment_id == assignment["id"])).mappings())
    assert len(rows) == 2
    assert sorted(row["mode"] for row in rows) == ["digest", "muted"]


def test_node_file_and_direct_mentions_are_selective_deduplicated_and_project_scoped(world):
    topic = discussion(world)
    run, direct = assigned(world)
    node_worker = world.assignment(run)
    file_worker = world.assignment(run)
    unrelated = world.assignment(run)
    node = create(world.conn, "node", project_id=world.project["id"], number=12, title="Convexity",
                  source_repository_id=world.workspace_repo["id"], source_path="Convexity.lean", source_commit_oid="a" * 40)
    subscribe(world.conn, world.actor, SubscriptionCreate(assignment_id=node_worker["id"],
        subject={"kind": "node", "id": node["id"]}), world.service)
    subscribe(world.conn, world.actor, SubscriptionCreate(assignment_id=file_worker["id"],
        subject={"kind": "directory", "repository_id": world.workspace_repo["id"], "path": "Math"}), world.service)
    subscribe(world.conn, world.actor, SubscriptionCreate(assignment_id=direct["id"],
        subject={"kind": "discussion", "id": topic["id"]}, mode="muted"), world.service)
    value, event = message(world, topic, f"@N12 check @workspace:Math/Convex.lean with @R{run['number']}/A{direct['number']}.")
    route_message(world.conn, event, topic["id"])
    route_message(world.conn, event, topic["id"])
    notices = list(world.conn.execute(select(tables["notification"]).where(tables["notification"].c.event_id == event["id"])).mappings())
    assert {row["assignment_id"] for row in notices} == {node_worker["id"], file_worker["id"], direct["id"]}
    assert next(row["urgency"] for row in notices if row["assignment_id"] == direct["id"]) == "direct"
    assert len(list(world.conn.execute(select(tables["message_reference"]).where(
        tables["message_reference"].c.message_id == value["id"])))) == 3
    other = create(world.conn, "project", slug="different-project", title="Different")
    assert resolve_mentions(world.conn, other["id"], f"@R{run['number']}/A{direct['number']} @N12 @workspace:Math/Convex.lean") == (set(), set())


def test_fenced_examples_do_not_route_and_discussion_links_inherit_subject(world):
    topic = discussion(world)
    run, assignment = assigned(world)
    node = create(world.conn, "node", project_id=world.project["id"], number=12, title="Definition",
                  source_repository_id=world.workspace_repo["id"], source_path="Definition.lean", source_commit_oid="a" * 40)
    subscribe(world.conn, world.actor, SubscriptionCreate(assignment_id=assignment["id"],
        subject={"kind": "node", "id": node["id"]}), world.service)
    assert resolve_mentions(world.conn, world.project["id"], "```lean\n@N12\n```\n`@N12`\n~~~\n@N12\n~~~") == (set(), set())
    link_subject(world.conn, world.actor, DiscussionSubjectLink(discussion_id=topic["id"], subject={"kind": "node", "id": node["id"]}))
    _, event = message(world, topic, "I found a missing hypothesis.")
    route_message(world.conn, event, topic["id"])
    assert world.conn.execute(select(tables["notification"].c.assignment_id).where(
        tables["notification"].c.event_id == event["id"])).scalar_one() == assignment["id"]


def test_delta_bootstrap_reuses_receipts_and_detects_old_edits_deletions_and_new_arrivals(world):
    topic = discussion(world)
    _, assignment = assigned(world)
    rows = [create(world.conn, "message", discussion_id=topic["id"], remote_id=str(number),
        remote_author_id="author", body=f"Message {number}", posted_at=datetime.now(timezone.utc))
        for number in range(1, 61)]
    def delta():
        return discussion_messages(world.conn, world.actor, topic["id"], None, 20, assignment["id"], unread_only=True)
    initial = delta()
    assert initial["unread_count"] == 20
    assert initial["older_history_available"]
    assert {int(row["remote_id"]) for row in initial["items"]} == set(range(41, 61))
    read_messages(world.conn, world.actor, assignment["id"], initial["read_revisions"], world.service)
    assert delta()["unread_count"] == 0
    queued = queue_reply(world.conn, world.actor, world.service, discussion_id=topic["id"],
        assignment_id=assignment["id"], body="A concrete question", read_revisions=[], urgent=False, key="delta-reuse")
    assert len(queued["payload"]["read_messages"]) == 20
    assert queued["payload"]["read_start_remote_id"] == 41
    change(world.conn, "message", rows[40]["id"], body="Corrected earlier definition")
    change(world.conn, "message", rows[41]["id"], deleted_at=datetime.now(timezone.utc))
    create(world.conn, "message", discussion_id=topic["id"], remote_id="61", remote_author_id="peer",
           body="New answer", posted_at=datetime.now(timezone.utc))
    changed = delta()
    assert {int(row["remote_id"]) for row in changed["items"]} == {41, 42, 61}
    assert changed["fingerprint"] != initial["fingerprint"]
    assert any(row["deleted_at"] for row in changed["items"])
    with pytest.raises(DomainError, match="Read the relevant discussion"):
        queue_reply(world.conn, world.actor, world.service, discussion_id=topic["id"],
            assignment_id=assignment["id"], body="Stale response", read_revisions=[], urgent=False, key="stale-delta")
    read_messages(world.conn, world.actor, assignment["id"], changed["read_revisions"], world.service)
    assert delta()["unread_count"] == 0


def test_delta_keeps_old_direct_address_and_per_assignment_receipts(world):
    topic = discussion(world)
    run, first = assigned(world)
    second = world.assignment(run)
    old, event = message(world, topic, f"@R{run['number']}/A{first['number']} which definition should we use?")
    route_message(world.conn, event, topic["id"])
    for number in range(2, 32):
        create(world.conn, "message", discussion_id=topic["id"], remote_id=str(number), remote_author_id="author",
               body="Later discussion", posted_at=datetime.now(timezone.utc))
    first_view = discussion_messages(world.conn, world.actor, topic["id"], None, 20, first["id"], unread_only=True)
    second_view = discussion_messages(world.conn, world.actor, topic["id"], None, 20, second["id"], unread_only=True)
    assert first_view["unread_count"] == 21
    assert second_view["unread_count"] == 20
    read_messages(world.conn, world.actor, first["id"], first_view["read_revisions"], world.service)
    remainder = discussion_messages(world.conn, world.actor, topic["id"], None, 20, first["id"], unread_only=True)
    assert [row["id"] for row in remainder["items"]] == [old["id"]]
    assert discussion_messages(world.conn, world.actor, topic["id"], None, 20, second["id"], unread_only=True)["unread_count"] == 20


def test_participation_follows_topic_preserving_explicit_mute_and_registration_links(world):
    topic = discussion(world)
    _, assignment = assigned(world)
    linked = register_discussion(world.conn, world.actor, DiscussionRegistration(project_id=world.project["id"],
        source_discussion_id=topic["id"], topic="Definition design", subjects=[{"kind": "mission", "id": world.mission["id"]}]))
    assert world.conn.execute(select(tables["discussion_subject"]).where(
        tables["discussion_subject"].c.discussion_id == linked["id"])).first()
    subscribe(world.conn, world.actor, SubscriptionCreate(assignment_id=assignment["id"],
        subject={"kind": "discussion", "id": linked["id"]}, mode="muted"), world.service)
    value, _ = message(world, linked, "Question")
    read_messages(world.conn, world.actor, assignment["id"], [{"id": value["id"], "revision": 1}], world.service)
    reference = object_ref(world.conn, "discussion", linked["id"])
    assert world.conn.execute(select(tables["subscription"].c.mode).where(
        tables["subscription"].c.assignment_id == assignment["id"], tables["subscription"].c.subject_id == reference)).scalar_one() == "muted"


def test_agents_cannot_bypass_read_safety_with_urgent_flag(world):
    topic = discussion(world)
    _, assignment = assigned(world)
    worker = authenticate(world.conn, world.claim()["execution_token"])
    with pytest.raises(DomainError, match="administrator"):
        queue_reply(world.conn, worker, world.service, discussion_id=topic["id"], assignment_id=assignment["id"],
                    body="Read bypass", read_revisions=[], urgent=True, key="bypass")


def test_discussion_checkpoint_observes_old_edits_deletions_and_topic_rename(world):
    from archon_horizon.pipeline.communications import discussion_fingerprint
    from archon_horizon.pipeline.conditions import Truth
    topic = discussion(world)
    topic = change(world.conn, "discussion", topic["id"], sync_status="current")
    run, _ = assigned(world)
    value, _ = message(world, topic, "Definition")
    def waiting():
        return world.assignment(run, start_condition={"version": 1, "expression": {
            "op": "discussion_changed", "discussion_id": str(topic["id"]),
            "fingerprint": discussion_fingerprint(world.conn, topic["id"])}})
    def ready(assignment):
        return world.service.readiness(world.conn, assignment, datetime.now(timezone.utc))
    assignment = waiting()
    assert ready(assignment).truth is Truth.FALSE
    change(world.conn, "message", value["id"], body="Generalized definition")
    assert ready(assignment).ready
    assignment = waiting()
    change(world.conn, "message", value["id"], deleted_at=datetime.now(timezone.utc))
    assert ready(assignment).ready
    assignment = waiting()
    change(world.conn, "discussion", topic["id"], topic="Definition decision")
    assert ready(assignment).ready
    change(world.conn, "discussion", topic["id"], sync_status="reconciling")
    assert ready(assignment).truth is Truth.UNKNOWN


def test_initial_read_is_atomic_across_partial_pages_and_large_arrival_burst(world):
    topic = discussion(world)
    _, assignment = assigned(world)
    def add(number):
        return create(world.conn, "message", discussion_id=topic["id"], remote_id=str(number),
            remote_author_id="author", body=f"Message {number}", posted_at=datetime.now(timezone.utc))
    for number in range(1, 31):
        add(number)
    page = discussion_messages(world.conn, world.actor, topic["id"], None, 5, assignment["id"], unread_only=True)
    with pytest.raises(DomainError, match="initial window together"):
        read_messages(world.conn, world.actor, assignment["id"], page["read_revisions"], world.service)
    initial = discussion_messages(world.conn, world.actor, topic["id"], None, 20, assignment["id"], unread_only=True)
    for number in range(31, 62):
        add(number)
    with pytest.raises(DomainError, match="initial window together"):
        read_messages(world.conn, world.actor, assignment["id"], initial["read_revisions"], world.service)
    assert world.conn.execute(select(tables["message_read"]).where(
        tables["message_read"].c.assignment_id == assignment["id"])).first() is None
    latest = discussion_messages(world.conn, world.actor, topic["id"], None, 20, assignment["id"], unread_only=True)
    read_messages(world.conn, world.actor, assignment["id"], initial["read_revisions"] + latest["read_revisions"], world.service)
    remaining = discussion_messages(world.conn, world.actor, topic["id"], None, 20, assignment["id"], unread_only=True)
    assert {int(row["remote_id"]) for row in remaining["items"]} == set(range(31, 42))
