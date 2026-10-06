from __future__ import annotations

import pytest

from archon_horizon.pipeline import models
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.auth import authenticate
from archon_horizon.pipeline.commands import Command, execute
from archon_horizon.pipeline.mission_tree import scope_contains
from archon_horizon.pipeline.schema import tables
from sqlalchemy import select
from archon_horizon.pipeline.records import get
from archon_horizon.pipeline.records import create
from test_pipeline_service import service_database, world  # noqa: F401


def child(world, parent, *, scope=None, budget=None):
    parent = get(world.conn, "mission", parent["id"])
    return world.service.mission(world.conn, world.actor, models.MissionCreate(
        project_id=world.project["id"], parent_id=parent["id"],
        expected_parent_revision=parent["revision"], title="Narrow task",
        objective="Resolve one bounded part", acceptance_criteria=["A checked result"],
        delegation_note="This child owns only the stated bounded task", scope=scope,
        max_open_children=budget or 8))


def test_child_contract_is_revision_checked_and_scope_is_narrowed(world):
    root = world.service.update_mission(world.conn, world.actor, world.mission["id"], models.MissionUpdate(
        expected_revision=world.mission["revision"], scope=models.MissionScope(
            repository_paths=[models.MissionRepositoryScope(repository_id=world.workspace_repo["id"], path="Math")]),
        acceptance_criteria=["The parent route is validated"]))
    scoped = child(world, root, scope=models.MissionScope(repository_paths=[
        models.MissionRepositoryScope(repository_id=world.workspace_repo["id"], path="Math/Foo")]))
    assert scoped["scope"]["repository_paths"][0]["path"] == "Math/Foo"
    current = get(world.conn, "mission", root["id"])
    with pytest.raises(DomainError, match="parent mission changed"):
        world.service.mission(world.conn, world.actor, models.MissionCreate(
            project_id=world.project["id"], parent_id=root["id"], expected_parent_revision=root["revision"],
            title="Stale", objective="Stale", acceptance_criteria=["x"], delegation_note="stale"))
    with pytest.raises(DomainError, match="scope"):
        child(world, current, scope=models.MissionScope(repository_paths=[
            models.MissionRepositoryScope(repository_id=world.workspace_repo["id"], path="Other")]))


def test_child_budget_and_cycle_are_rejected(world):
    parent = world.service.update_mission(world.conn, world.actor, world.mission["id"], models.MissionUpdate(
        expected_revision=world.mission["revision"], max_open_children=1))
    first = child(world, parent)
    parent = get(world.conn, "mission", parent["id"])
    with pytest.raises(DomainError, match="budget") as error:
        child(world, parent)
    assert error.value.code == "child_budget_exhausted"
    assert error.value.details == {
        "parent_id": str(parent["id"]), "parent_revision": parent["revision"],
        "parent_detail_url": f"/api/v3/records/mission/{parent['id']}",
        "max_open_children": 1, "open_count": 1,
        "open_children": [{
            "id": str(first["id"]), "number": first["number"], "title": first["title"],
            "status": "open", "revision": first["revision"],
            "detail_url": f"/api/v3/records/mission/{first['id']}",
        }],
        "open_children_truncated": False, "update_contract": "MissionUpdate",
        "update_url": f"/api/v3/missions/{parent['id']}",
        "schema_url": "/api/v3/schema?section=mission_update",
    }
    with pytest.raises(DomainError, match="descendant"):
        world.service.update_mission(world.conn, world.actor, parent["id"], models.MissionUpdate(
            expected_revision=get(world.conn, "mission", parent["id"])["revision"],
            parent_id=first["id"], expected_parent_revision=first["revision"]))
    parent = world.service.update_mission(world.conn, world.actor, parent["id"], models.MissionUpdate(
        expected_revision=parent["revision"], max_open_children=2))
    assert child(world, parent)["parent_id"] == parent["id"]
    assert get(world.conn, "mission", first["id"])["status"] == "open"


def test_parent_cannot_close_until_children_are_settled(world):
    parent = get(world.conn, "mission", world.mission["id"])
    first = child(world, parent)
    parent = get(world.conn, "mission", parent["id"])
    with pytest.raises(DomainError, match="open child") as error:
        world.command("complete_mission", parent, note="Premature completion")
    assert error.value.code == "open_children"
    assert error.value.details["blocking_mission_count"] == 1
    assert error.value.details["blocking_missions"] == [{
        "id": str(first["id"]), "number": first["number"], "title": first["title"],
        "status": "open", "revision": first["revision"], "parent_id": str(parent["id"]),
        "detail_url": f"/api/v3/records/mission/{first['id']}",
    }]
    assert error.value.details["blocking_missions_truncated"] is False
    world.command("complete_mission", first, note="Child evidence is complete")
    parent = get(world.conn, "mission", parent["id"])
    completed = world.command("complete_mission", parent, note="All delegated evidence is complete")
    assert completed["status"] == "completed"


def test_agent_can_delegate_only_inside_owned_tree(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    agent = authenticate(world.conn, claim["execution_token"])
    root = get(world.conn, "mission", world.mission["id"])
    delegated = world.service.mission(world.conn, agent, models.MissionCreate(
        project_id=world.project["id"], parent_id=root["id"], expected_parent_revision=root["revision"],
        title="Agent child", objective="A bounded child", acceptance_criteria=["Evidence is recorded"],
        delegation_note="The child owns the bounded evidence"))
    unrelated = world.service.mission(world.conn, world.actor, models.MissionCreate(
        project_id=world.project["id"], title="Unrelated", objective="A separate root"))
    with pytest.raises(DomainError, match="outside this execution"):
        world.service.mission(world.conn, agent, models.MissionCreate(
            project_id=world.project["id"], parent_id=unrelated["id"], expected_parent_revision=unrelated["revision"],
            title="Escaped", objective="Should be rejected", acceptance_criteria=["No"], delegation_note="No"))
    with pytest.raises(DomainError, match="delegation requires"):
        world.service.assignment(world.conn, agent, models.AssignmentCreate(
            run_id=run["id"], mission_id=delegated["id"], role="worker"))


def test_agent_cannot_reparent_its_authority_root_or_edit_sibling(world):
    run = world.run()
    world.disable_automations(run)
    owned = child(world, world.mission)
    sibling = child(world, world.mission)
    assignment = world.service.assignment(world.conn, world.actor, models.AssignmentCreate(
        run_id=run["id"], mission_id=owned["id"]))
    claim = world.claim()
    assert claim["assignment_id"] == str(assignment["id"])
    agent = authenticate(world.conn, claim["execution_token"])
    with pytest.raises(DomainError, match="authority root"):
        world.service.update_mission(world.conn, agent, owned["id"], models.MissionUpdate(
            expected_revision=owned["revision"], parent_id=None))
    with pytest.raises(DomainError, match="delegated subtree"):
        world.service.update_mission(world.conn, agent, sibling["id"], models.MissionUpdate(
            expected_revision=sibling["revision"], objective="Stolen sibling work"))
    sibling_assignment = world.service.assignment(world.conn, world.actor, models.AssignmentCreate(
        run_id=run["id"], mission_id=sibling["id"]))
    with pytest.raises(DomainError, match="delegated subtree"):
        execute(world.conn, agent, Command(operation="cancel_assignment", target_id=sibling_assignment["id"],
            expected_revision=sibling_assignment["revision"], args={"note": "Unauthorized sibling cancellation"}),
            world.service, world.scheduler)


def test_child_scope_cannot_be_cleared_or_exclude_existing_descendant(world):
    root = world.service.update_mission(world.conn, world.actor, world.mission["id"], models.MissionUpdate(
        expected_revision=world.mission["revision"], scope={"repository_paths": [
            {"repository_id": world.workspace_repo["id"], "path": "Math"}]}))
    owned = child(world, root, scope={"repository_paths": [
        {"repository_id": world.workspace_repo["id"], "path": "Math/Foo"}]})
    with pytest.raises(DomainError, match="scope"):
        world.service.update_mission(world.conn, world.actor, owned["id"], models.MissionUpdate(
            expected_revision=owned["revision"], scope={}))
    root = get(world.conn, "mission", root["id"])
    with pytest.raises(DomainError, match="existing child"):
        world.service.update_mission(world.conn, world.actor, root["id"], models.MissionUpdate(
            expected_revision=root["revision"], scope={"repository_paths": [
            {"repository_id": world.workspace_repo["id"], "path": "Math/Bar"}]}))


def test_explicit_scope_contains_mission_graph_links(world):
    node = create(world.conn, "node", project_id=world.project["id"], number=1, title="Bounded node",
                  source_repository_id=world.workspace_repo["id"], source_path="Math/node.md", source_commit_oid="a" * 40)
    scoped = world.service.mission(world.conn, world.actor, models.MissionCreate(
        project_id=world.project["id"], title="Linked scope", objective="Use one graph slice",
        node_ids=[node["id"]], document_ids=[world.document["id"]], scope=models.MissionScope(
            node_ids=[node["id"]], document_ids=[world.document["id"]])))
    assert str(node["id"]) in scoped["scope"]["node_ids"]
    with pytest.raises(DomainError, match="node links"):
        world.service.mission(world.conn, world.actor, models.MissionCreate(
            project_id=world.project["id"], title="Mismatched scope", objective="Reject this",
            node_ids=[node["id"]], scope=models.MissionScope(document_ids=[world.document["id"]])))


def test_assignments_are_owned_once_and_remain_within_run_tree(world):
    run = world.run()
    world.disable_automations(run)
    owned = child(world, world.mission)
    world.service.assignment(world.conn, world.actor, models.AssignmentCreate(run_id=run["id"], mission_id=owned["id"]))
    with pytest.raises(DomainError, match="active assignment"):
        world.service.assignment(world.conn, world.actor, models.AssignmentCreate(run_id=run["id"], mission_id=owned["id"]))
    foreign = world.service.mission(world.conn, world.actor, models.MissionCreate(
        project_id=world.project["id"], title="Another root", objective="Separate run"))
    with pytest.raises(DomainError, match="run's mission tree"):
        world.service.assignment(world.conn, world.actor, models.AssignmentCreate(run_id=run["id"], mission_id=foreign["id"]))


def test_run_coordinators_own_root_and_can_reach_descendants(world):
    run = world.run()
    automations = list(world.conn.execute(select(tables["automation"]).where(
        tables["automation"].c.run_id == run["id"])).mappings())
    assert {row["mission_id"] for row in automations} == {world.mission["id"]}
    assert {row["role"] for row in automations} == {"worker", "maintainer"}


def test_reopening_child_cannot_bypass_budget(world):
    root = world.service.update_mission(world.conn, world.actor, world.mission["id"], models.MissionUpdate(
        expected_revision=world.mission["revision"], max_open_children=1))
    first = child(world, root)
    cancelled = world.command("cancel_mission", first, note="Replace this direction")
    child(world, root)
    with pytest.raises(DomainError, match="budget"):
        world.command("reopen_mission", cancelled, note="Cannot create another active child")


@pytest.mark.parametrize("path,expected", [("Math/Foo", True), ("Math/Foobar", True), ("Mathematics", False), (None, False)])
def test_repository_scope_uses_path_segments(path, expected):
    parent = {"repository_paths": [{"repository_id": "repository", "path": "Math"}]}
    assert scope_contains(parent, {"repository_paths": [{"repository_id": "repository", "path": path}]}) is expected
