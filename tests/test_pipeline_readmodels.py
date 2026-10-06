from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import insert, update

from archon_horizon.pipeline import models, readmodels
from archon_horizon.pipeline.records import create
from archon_horizon.pipeline.schema import tables
from test_pipeline_service import service_database, world


def test_event_replay_floor_excludes_old_retained_semantic_history(service_database):
    now = datetime.now(timezone.utc)
    with service_database.engine.connect() as conn:
        transaction = conn.begin()
        try:
            assert readmodels.replay_window(conn, 3600) == (0, 0)
            old = create(conn, "event", kind="decision", schema_version=1, source="test",
                source_event_id=str(uuid4()), occurred_at=now - timedelta(days=30),
                created_at=now - timedelta(days=30), payload={})
            assert readmodels.replay_window(conn, 3600) == (old["sequence"], old["sequence"])
            recent = create(conn, "event", kind="record_changed", schema_version=1, source="test",
                source_event_id=str(uuid4()), occurred_at=now, payload={})
            assert readmodels.replay_window(conn, 3600) == (recent["sequence"] - 1, recent["sequence"])
        finally:
            transaction.rollback()


def test_assignment_search_resolves_display_reference_and_source_path(world):
    run = world.run()
    assignment = world.assignment(run)
    result = readmodels.assignments(world.conn, world.actor, world.service, run["id"], None, 30,
                                    f"R{run['number']}/A{assignment['number']}")
    assert [row["id"] for row in result["items"]] == [assignment["id"]]
    node = create(world.conn, "node", project_id=world.project["id"], number=1, title="Convexity",
                  source_repository_id=world.workspace_repo["id"], source_path="Geometry/Convexity.lean", source_commit_oid="a" * 40)
    world.conn.execute(insert(tables["mission_node"]).values(mission_id=world.mission["id"], node_id=node["id"]))
    result = readmodels.assignments(world.conn, world.actor, world.service, run["id"], None, 30, "Convexity.lean")
    assert assignment["id"] in {row["id"] for row in result["items"]}
    assert readmodels.assignments(world.conn, world.actor, world.service, run["id"], None, 30, "unrelated")["items"] == []


def test_roadmap_uses_adopted_snapshot_not_newest_project_snapshot(world):
    artifact = create(world.conn, "artifact", project_id=world.project["id"], kind="blob",
                      content={"sha256": "a" * 64, "size_bytes": 1, "media_type": "application/json"})
    def baseline(commit):
        return create(world.conn, "roadmap_snapshot", project_id=world.project["id"], roadmap_document_id=world.document["id"],
                      source_commit_oid=commit * 40, graph_manifest_artifact_id=artifact["id"], status="provisional")
    adopted = baseline("a")
    run = world.scheduler.run(world.conn, world.actor, models.RunCreate(mission_id=world.mission["id"],
        phase={"kind": "formalization", "roadmap_snapshot_id": adopted["id"]}, host_ids=[world.host["id"]]))
    baseline("b")
    result = readmodels.roadmap(world.conn, world.actor, world.project["id"], None, 30, run["id"])
    assert result["baseline"]["id"] == adopted["id"]
    assert readmodels.roadmap(world.conn, world.actor, world.project["id"], None, 30)["baseline"] is None


def test_forge_changes_have_independent_cursor_and_human_links(world):
    world.conn.execute(update(tables["repository"]).where(tables["repository"].c.id == world.workspace_repo["id"]).values(remote_path="geometry/workspace"))
    for number in range(1, 4):
        create(world.conn, "forge_item", repository_id=world.workspace_repo["id"], remote_number=number,
               kind="pull_request", title=f"Lemma {number}", status="open", head_commit_oid="a" * 40,
               review_phase="formalization", observed_at=datetime.now(timezone.utc))
    first = readmodels.forge_items(world.conn, world.actor, world.project["id"], None, 2)
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = readmodels.forge_items(world.conn, world.actor, world.project["id"], first["next_cursor"], 2)
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    assert {row["id"] for row in first["items"]}.isdisjoint({row["id"] for row in second["items"]})
    assert first["items"][0]["url"].startswith("https://forge.invalid/geometry/workspace/pulls/")
    readiness = first["items"][0]["review_readiness"]
    assert readiness["forge_item_id"] == str(first["items"][0]["id"])
    assert readiness["head_commit_oid"] == "a" * 40
    assert readiness["ready"] is False
    assert readiness["blockers"] == [{"code": "review_policy_missing"}]


def test_discussion_list_omits_message_bodies_and_pages_history(world):
    run = world.run()
    assignment = world.assignment(run)
    integration = create(world.conn, "integration", kind="zulip", endpoint="https://zulip.invalid", credential_ref="secret:test")
    discussion = create(world.conn, "discussion", project_id=world.project["id"], integration_id=integration["id"],
                        channel_remote_id="1", topic="Statement alignment", observed_at=datetime.now(timezone.utc))
    subject = create(world.conn, "object_reference", kind="discussion", discussion_id=discussion["id"])
    create(world.conn, "subscription", assignment_id=assignment["id"], subject_id=subject["id"], mode="digest", origin="explicit")
    for number in range(3):
        create(world.conn, "message", discussion_id=discussion["id"], remote_id=str(number), remote_author_id="Maintainer",
               body=f"Message {number}", posted_at=datetime.now(timezone.utc) + timedelta(seconds=number))
    topics = readmodels.discussions(world.conn, world.actor, world.project["id"], None, 20, assignment["id"])
    assert topics["items"][0]["unread_count"] == 3
    assert topics["items"][0]["subscribed"]
    assert "messages" not in topics["items"][0]
    recent = readmodels.discussion_messages(world.conn, world.actor, discussion["id"], None, 2)
    assert [row["content"] for row in recent["items"]] == ["Message 2", "Message 1"]
    earlier = readmodels.discussion_messages(world.conn, world.actor, discussion["id"], recent["next_cursor"], 2)
    assert [row["content"] for row in earlier["items"]] == ["Message 0"]


def test_resources_excludes_disabled_harness_capacity(world):
    before = readmodels.resources(world.conn, world.actor, world.service.config)
    host = next(row for row in before["hosts"] if row["id"] == world.host["id"])
    assert host["slots"] == 2
    world.conn.execute(update(tables["harness"]).where(tables["harness"].c.id == world.harness["id"]).values(enabled=False))
    after = readmodels.resources(world.conn, world.actor, world.service.config)
    host = next(row for row in after["hosts"] if row["id"] == world.host["id"])
    assert host["slots"] == 0
