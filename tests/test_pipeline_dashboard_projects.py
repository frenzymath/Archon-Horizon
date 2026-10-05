import pytest

from archon_horizon.pipeline import dashboard_projects, models
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.records import get
from archon_horizon.pipeline.roadmap_index import index_snapshot
from test_pipeline_service import service_database, world


def seed(world):
    index_snapshot(world.conn, world.actor, world.document["source_repository_id"], "b" * 40, {
        "nodes/Geometry/main.md": "---\ntitle: Main theorem\nlabels: [formally_stated]\nchildren: [helper]\n---\n\nMathematical statement",
        "nodes/helper.md": "---\nlabel: helper\ntitle: Helper lemma\nlabels: [informal_stated]\n---\n\nAuxiliary proof",
        "objectives/main.md": "---\ntitle: Formalization strategy\n---\n\n- [x] [[node:main]]",
    })


def test_project_directories_are_bounded_and_details_resolve_source_keys(world):
    seed(world)
    result = dashboard_projects.nodes(world.conn, world.actor, world.project["id"], world.service.config, limit=1)
    assert result["total"] == 2 and len(result["nodes"]) == 1
    assert "markdown" not in result["nodes"][0]
    filtered = dashboard_projects.nodes(world.conn, world.actor, world.project["id"], world.service.config, label="formally_stated")
    assert [row["title"] for row in filtered["nodes"]] == ["Main theorem"]
    detail = dashboard_projects.node_detail(world.conn, world.actor, world.project["id"], "main", world.service.config)
    assert detail["node"]["markdown"] == "Mathematical statement"
    assert len(detail["nodes"]) == 2 and len(detail["node"]["children"]) == 1
    by_id = dashboard_projects.node_detail(world.conn, world.actor, world.project["id"], str(detail["node"]["id"]), world.service.config)
    assert by_id == detail
    summaries = dashboard_projects.node_summaries(world.conn, world.actor, world.project["id"], ["main", "helper"], world.service.config)
    assert len(summaries["nodes"]) == 2


def test_project_graph_has_real_edges_and_document_detail_is_separate(world):
    seed(world)
    graph = dashboard_projects.graph(world.conn, world.actor, world.project["id"], world.service.config)
    assert len(graph["nodes"]) == 2 and sum(len(node["children"]) for node in graph["nodes"]) == 1
    assert all("markdown" not in node for node in graph["nodes"])
    helper = next(node for node in graph["nodes"] if node["label"] == "helper")
    focus = dashboard_projects.graph(world.conn, world.actor, world.project["id"], world.service.config, str(helper["id"]))
    assert len(focus["nodes"]) == 1
    objectives = dashboard_projects.objectives(world.conn, world.actor, world.project["id"], world.service.config)
    objective = next(item for item in objectives["items"] if item["title"] == "Formalization strategy")
    assert "markdown" not in objective
    detail = dashboard_projects.objectives(world.conn, world.actor, world.project["id"], world.service.config, objective["id"])
    assert detail["markdown"] == "- [x] [[node:main]]"


def test_project_node_lookup_never_crosses_project_scope(world):
    seed(world)
    other = world.service.project(world.conn, world.actor, models.ProjectCreate(slug="unrelated-project", title="Other project"))
    with pytest.raises(DomainError, match="not found"):
        dashboard_projects.node_detail(world.conn, world.actor, other["id"], "main", world.service.config)
    assert dashboard_projects.node_summaries(world.conn, world.actor, other["id"], ["main"], world.service.config)["nodes"] == []


def test_mission_pages_and_search_include_ancestors_without_counting_them_as_matches(world):
    child = world.service.mission(world.conn, world.actor, models.MissionCreate(project_id=world.project["id"],
        parent_id=world.mission["id"], expected_parent_revision=world.mission["revision"],
        acceptance_criteria=["Construct intermediate geometry"], delegation_note="Own intermediate geometry",
        title="Child construction", objective="Intermediate geometry"))
    leaf = world.service.mission(world.conn, world.actor, models.MissionCreate(project_id=world.project["id"],
        parent_id=child["id"], expected_parent_revision=child["revision"],
        acceptance_criteria=["The curvature bound is checked"], delegation_note="Own only the curvature bound",
        title="Leaf estimate", objective="Bound the curvature"))
    page = dashboard_projects.missions(world.conn, world.actor, world.project["id"], offset=2, limit=1)
    assert page["match_ids"] == [leaf["id"]]
    assert {item["id"] for item in page["items"]} == {world.mission["id"], child["id"], leaf["id"]}
    assert page["total"] == 3 and page["next_offset"] is None
    assert all("objective" not in item for item in page["items"])
    search = dashboard_projects.missions(world.conn, world.actor, world.project["id"], search="curvature", limit=1)
    assert search["match_ids"] == [leaf["id"]] and search["total"] == 1
    assert len(search["items"]) == 3
    root = next(item for item in page["items"] if item["id"] == world.mission["id"])
    assert root["child_count"] == 1
    assert dashboard_projects.mission_detail(world.conn, world.actor, world.project["id"], leaf["id"])["parent_title"] == child["title"]


def test_reparent_mission_rejects_cycles_cross_project_and_preserves_revision_guards(world):
    child = world.service.mission(world.conn, world.actor, models.MissionCreate(project_id=world.project["id"],
        parent_id=world.mission["id"], expected_parent_revision=world.mission["revision"],
        acceptance_criteria=["The child result is checked"], delegation_note="Own the child result",
        title="Child", objective="Child objective"))
    for parent in (world.mission["id"], child["id"]):
        with pytest.raises(DomainError, match="descendants"):
            world.service.update_mission(world.conn, world.actor, world.mission["id"],
                models.MissionUpdate(expected_revision=get(world.conn, "mission", world.mission["id"])["revision"],
                    parent_id=parent, expected_parent_revision=get(world.conn, "mission", parent)["revision"]))
    other = world.service.project(world.conn, world.actor, models.ProjectCreate(slug="other_missions", title="Other"))
    foreign = world.service.mission(world.conn, world.actor, models.MissionCreate(project_id=other["id"], title="Foreign", objective="Other project"))
    with pytest.raises(DomainError, match="same project"):
        world.service.update_mission(world.conn, world.actor, child["id"], models.MissionUpdate(expected_revision=1,
            parent_id=foreign["id"], expected_parent_revision=foreign["revision"]))
    detached = world.service.update_mission(world.conn, world.actor, child["id"], models.MissionUpdate(expected_revision=1, parent_id=None, title="Independent"))
    assert detached["parent_id"] is None and detached["title"] == "Independent"
    with pytest.raises(DomainError, match="changed"):
        world.service.update_mission(world.conn, world.actor, child["id"], models.MissionUpdate(expected_revision=1, title="Stale draft"))
    preserved = dashboard_projects.mission_detail(world.conn, world.actor, world.project["id"], child["id"])
    assert preserved["title"] == "Independent"


def test_reparent_uses_existing_mission_goal_update_path(world):
    from sqlalchemy import select
    from archon_horizon.pipeline.schema import tables

    parent = world.service.mission(world.conn, world.actor, models.MissionCreate(project_id=world.project["id"], title="Parent", objective="Global strategy"))
    run = world.run()
    world.disable_automations(run)
    world.assignment(run)
    claim = world.claim()
    assert claim
    changed = world.service.update_mission(world.conn, world.actor, world.mission["id"],
        models.MissionUpdate(expected_revision=get(world.conn, "mission", world.mission["id"])["revision"],
            parent_id=parent["id"], expected_parent_revision=parent["revision"], objective="Refined original objective",
            acceptance_criteria=["The refined objective is established"], delegation_note="Refine this result under the global strategy"))
    assert changed["status"] == "open" and changed["parent_id"] == parent["id"]
    outbox = tables["outbox_operation"]
    update = world.conn.execute(select(outbox).where(outbox.c.kind == "goal_update")).mappings().first()
    assert update and update["payload"]["provider_thread_id"] == str(claim["provider_thread_record_id"])
