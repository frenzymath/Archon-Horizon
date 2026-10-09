from uuid import uuid4

import pytest
import yaml
from sqlalchemy import select

from archon_horizon.pipeline.dashboard import dashboard_projects, readmodels
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.graph_progress import fingerprint, validate_implementations
from archon_horizon.pipeline.persistence.records import create
from archon_horizon.pipeline.roadmap_index import index_snapshot
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_service import service_database, world


def library(world):
    return create(world.conn, "repository", project_id=world.project["id"], slug="library",
        integration_id=world.workspace_repo["integration_id"], remote_id="library", purpose="library", default_branch="main")


def document(metadata, body="The statement"):
    return "---\n" + yaml.safe_dump(metadata) + "---\n\n" + body


def test_same_nodes_have_independent_progress_and_rewrite_invalidates_claims(world):
    target = library(world)
    metadata = {"label": "result", "title": "Result", "labels": ["formally_proved"], "children": []}
    digest = fingerprint(metadata, "The statement")
    metadata["implementations"] = {
        str(world.workspace_repo["id"]): {"status": "formally_proved", "node_sha256": digest, "commit_oid": "a" * 40},
        str(target["id"]): {"status": "formally_stated", "review": "statement_accepted", "node_sha256": digest}}
    repo = world.document["source_repository_id"]
    index_snapshot(world.conn, world.actor, repo, "b" * 40, {"nodes/result.md": document(metadata)})
    def view(target_id):
        return dashboard_projects.graph(world.conn, world.actor, world.project["id"], world.service.config,
            target_repository_id=target_id)["nodes"][0]
    source, destination = view(world.workspace_repo["id"]), view(target["id"])
    assert source["id"] == destination["id"]
    assert source["labels"] == ["formally_proved"]
    assert destination["labels"] == ["formally_stated"]
    assert destination["implementation"]["review"] == "statement_accepted"
    index_snapshot(world.conn, world.actor, repo, "c" * 40, {"nodes/result.md": document(metadata, "A different statement")})
    changed = view(target["id"])
    assert changed["labels"] == ["stale"]
    assert changed["implementation"]["review"] == "pending"
    assert changed["implementation"]["recorded"]["review"] == "statement_accepted"
    assert changed["id"] == source["id"]


def test_library_never_inherits_unscoped_workspace_labels_and_filters_correctly(world):
    target = library(world)
    index_snapshot(world.conn, world.actor, world.document["source_repository_id"], "b" * 40,
        {"nodes/result.md": document({"title": "Result", "labels": ["formally_proved"]})})
    result = dashboard_projects.nodes(world.conn, world.actor, world.project["id"], world.service.config,
        target_repository_id=target["id"], label="formally_proved")
    assert result["total"] == 0
    result = dashboard_projects.nodes(world.conn, world.actor, world.project["id"], world.service.config,
        target_repository_id=target["id"], label="open")
    assert result["total"] == 1
    agent = readmodels.roadmap(world.conn, world.actor, world.project["id"], None, 10, target_repository_id=target["id"])
    assert agent["items"][0]["status"] == "open"
    assert agent["target_repository_id"] == target["id"]


def test_target_dependencies_and_removal_preserve_historical_ids(world):
    target = library(world)
    repo = world.document["source_repository_id"]
    sources = {"nodes/main.md": document({"title": "Main", "children": ["helper"],
        "implementations": {str(target["id"]): {"children": []}}}),
        "nodes/helper.md": document({"title": "Helper"})}
    index_snapshot(world.conn, world.actor, repo, "a" * 40, sources)
    base = dashboard_projects.graph(world.conn, world.actor, world.project["id"], world.service.config)
    scoped = dashboard_projects.graph(world.conn, world.actor, world.project["id"], world.service.config,
        target_repository_id=target["id"])
    assert sum(len(row["children"]) for row in base["nodes"]) == 1
    assert sum(len(row["children"]) for row in scoped["nodes"]) == 0
    helper = next(row for row in scoped["nodes"] if row["title"] == "Helper")
    detail = dashboard_projects.node_detail(world.conn, world.actor, world.project["id"], str(helper["id"]),
        world.service.config, target_repository_id=target["id"])
    assert len(detail["nodes"]) == 1
    main = next(row for row in scoped["nodes"] if row["title"] == "Main")
    agent = readmodels.roadmap(world.conn, world.actor, world.project["id"], None, 10, target_repository_id=target["id"])
    assert next(row for row in agent["items"] if row["id"] == main["id"])["dependencies"] == []
    index_snapshot(world.conn, world.actor, repo, "b" * 40, {"nodes/main.md": document({"title": "Main"})})
    assert len(dashboard_projects.graph(world.conn, world.actor, world.project["id"], world.service.config)["nodes"]) == 1
    assert len(readmodels.roadmap(world.conn, world.actor, world.project["id"], None, 10)["items"]) == 1
    assert len(list(world.conn.execute(select(tables["node"])))) == 2


def test_invalid_target_and_cycles_reject_before_modifying_index(world):
    target = library(world)
    repo = world.document["source_repository_id"]
    for children in (["absent"], ["main"]):
        with pytest.raises(DomainError):
            index_snapshot(world.conn, world.actor, repo, "a" * 40, {"nodes/main.md": document({
                "title": "Main", "implementations": {str(target["id"]): {"children": children}}})})
    with pytest.raises(DomainError, match="project"):
        index_snapshot(world.conn, world.actor, repo, "a" * 40, {"nodes/main.md": document({
            "title": "Main", "implementations": {str(uuid4()): {"status": "open"}}})})
    with pytest.raises(DomainError, match="project"):
        dashboard_projects.graph(world.conn, world.actor, world.project["id"], world.service.config,
            target_repository_id=uuid4())
    assert world.conn.execute(select(tables["node"])).first() is None


@pytest.mark.parametrize("claim", [
    {"status": "formally_proved"}, {"review": "accepted"},
    {"status": "formally_proved", "node_sha256": "a" * 64},
    {"status": "typo"}, {"status": "open", "unexpected": True},
])
def test_invalid_progress_is_not_silently_accepted(claim):
    repo = uuid4()
    with pytest.raises(DomainError):
        validate_implementations({"implementations": {str(repo): claim}}, "Statement", {repo})
