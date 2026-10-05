from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from archon_horizon.pipeline import readmodels
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.integration_views import project_integrations
from archon_horizon.pipeline.roadmap_index import index_snapshot
from archon_horizon.pipeline.schema import tables
from test_pipeline_service import service_database, world


def source(title, children="[]", labels="[formally_stated]", body="Statement"):
    return f"---\ntitle: {title}\nchildren: {children}\nlabels: {labels}\ntype: theorem\n---\n\n{body}"


def test_index_preserves_source_evidence_dependencies_and_document(world):
    repo = world.document["source_repository_id"]
    sources = {"nodes/target.md": source("Target", "[helper]", "[formally_proved, conditionally_proved]"),
        "nodes/helper.md": source("Helper"), "objectives/main.md": source("Roadmap", body="- [x] [[node:target]]")}
    receipt = index_snapshot(world.conn, world.actor, repo, "b" * 40, sources)
    assert receipt["nodes"] == 2 and receipt["dependencies"] == 1
    result = readmodels.roadmap(world.conn, world.actor, world.project["id"], None, 30)
    target = next(row for row in result["items"] if row["title"] == "Target")
    helper = next(row for row in result["items"] if row["title"] == "Helper")
    assert target["dependencies"] == [helper["id"]]
    assert target["dependency_numbers"] == [helper["number"]]
    assert target["labels"] == ["formally_proved", "conditionally_proved"]
    assert target["status"] == "conditionally_proved"
    assert target["content"] == "Statement"
    assert result["summary"]["node_count"] == 2
    assert any(row["content"] == "- [x] [[node:target]]" for row in result["documents"])
    index_snapshot(world.conn, world.actor, repo, "b" * 40, sources)
    assert readmodels.roadmap(world.conn, world.actor, world.project["id"], None, 30)["items"][0]["id"] == target["id"]


def test_index_rejects_dangling_and_cyclic_dependencies_before_writing(world):
    repo = world.document["source_repository_id"]
    with pytest.raises(DomainError, match="absent"):
        index_snapshot(world.conn, world.actor, repo, "b" * 40, {"nodes/target.md": source("Target", "[missing]")})
    with pytest.raises(DomainError):
        index_snapshot(world.conn, world.actor, repo, "b" * 40, {
            "nodes/target.md": source("Target", "[helper]"), "nodes/helper.md": source("Helper", "[target]")})
    assert world.conn.execute(select(tables["node"])).first() is None


def test_source_metadata_is_not_used_for_another_commit(world):
    repo = world.document["source_repository_id"]
    index_snapshot(world.conn, world.actor, repo, "b" * 40, {"nodes/target.md": source("Target")})
    world.conn.execute(update(tables["node"]).values(source_commit_oid="c" * 40))
    node = readmodels.roadmap(world.conn, world.actor, world.project["id"], None, 30)["items"][0]
    assert node["labels"] == [] and node["status"] == "not_indexed"


def test_public_links_never_expose_internal_loopback_and_use_override(world):
    from datetime import datetime, timezone
    from archon_horizon.pipeline.records import create

    integration_id = world.workspace_repo["integration_id"]
    world.conn.execute(update(tables["integration"]).where(tables["integration"].c.id == integration_id)
        .values(endpoint="http://127.0.0.1:3000"))
    world.conn.execute(update(tables["repository"]).where(tables["repository"].c.id == world.workspace_repo["id"])
        .values(remote_path="project/workspace"))
    result = project_integrations(world.conn, world.actor, world.project["id"], world.service.config)
    assert result["items"][0]["public_url"] is None
    config = world.service.config.model_copy(update={"integration_public_urls": {integration_id: "https://forge.example"}})
    result = project_integrations(world.conn, world.actor, world.project["id"], config)
    assert result["items"][0]["public_url"] == "https://forge.example"
    assert next(repo for repo in result["items"][0]["repositories"] if repo["name"] == "workspace")["url"] == "https://forge.example/project/workspace"
    create(world.conn, "forge_item", repository_id=world.workspace_repo["id"], remote_number=7,
        kind="pull_request", title="Reviewed theorem", status="open", observed_at=datetime.now(timezone.utc))
    forge = readmodels.forge_items(world.conn, world.actor, world.project["id"], None, 20, config=config)
    assert forge["items"][0]["url"] == "https://forge.example/project/workspace/pulls/7"
    assert "endpoint" not in forge["items"][0]
    zulip = create(world.conn, "integration", kind="zulip", endpoint="http://127.0.0.1:34963", credential_ref="secret:test")
    create(world.conn, "discussion", project_id=world.project["id"], integration_id=zulip["id"],
        channel_remote_id="12", topic="Proof review", observed_at=datetime.now(timezone.utc))
    config.integration_public_urls[zulip["id"]] = "https://zulip.example"
    topics = readmodels.discussions(world.conn, world.actor, world.project["id"], None, 20, config=config)
    assert topics["items"][0]["url"] == "https://zulip.example/#narrow/stream/12/topic/Proof%20review"
    assert "endpoint" not in topics["items"][0]
    data = config.model_dump(mode="json")
    data["database_url"] = config.database_url.get_secret_value()
    assert type(config).model_validate(data).integration_public_urls == config.integration_public_urls
    data["integration_public_urls"] = {str(uuid4()): "https://secret:password@forge.example"}
    with pytest.raises(ValidationError):
        type(config).model_validate(data)
