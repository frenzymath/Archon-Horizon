"""Real PostgreSQL scale checks; plans are diagnostic, elapsed time is not a CI contract."""

import json
import re
from uuid import uuid4

from sqlalchemy import event, insert, select, text

from archon_horizon.pipeline.dashboard.readmodels import assignments
from archon_horizon.pipeline.persistence.records import get
from archon_horizon.pipeline.projects.references import identifier_expression
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_service import service_database, world  # noqa: F401


def test_thousand_assignment_queue_and_dashboard_queries(world, tmp_path):
    run = world.run()
    world.disable_automations(run)
    identifiers = [uuid4() for _ in range(1000)]
    world.conn.execute(insert(tables["assignment"]), [
        {"id": identifier, "run_id": run["id"], "mission_id": world.mission["id"],
         "number": index + 3, "queue_rank": (index + 3) * 1024}
        for index, identifier in enumerate(identifiers)])
    world.conn.execute(text("ANALYZE assignment"))
    captured = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith(("SELECT", "WITH")) and "assignment" in statement:
            captured.append((statement, parameters))

    event.listen(world.conn, "before_cursor_execute", capture)
    try:
        grant = world.claim()
        assert grant["assignment_id"] == str(identifiers[0])
        page = assignments(world.conn, world.actor, world.service, run["id"], None, 50)
        assert len(page["items"]) == 50
    finally:
        event.remove(world.conn, "before_cursor_execute", capture)
    plans = []
    for statement, parameters in captured:
        if "ORDER BY" not in statement:
            continue
        explain = world.conn.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters).scalar_one()[0]
        plans.append({"query": statement, "plan": explain})
    (tmp_path / "query-plans.json").write_text(json.dumps(plans, indent=2))
    summaries = [{"execution_ms": item["plan"]["Execution Time"],
                  "rows": item["plan"]["Plan"]["Actual Rows"],
                  "root": item["plan"]["Plan"]["Node Type"]} for item in plans]
    print(json.dumps({"queries": len(captured), "plans": summaries}))
    assert plans
    assert len(captured) < 100  # Read models batch a page instead of querying every row.
    def aggregates(plan):
        if plan["Node Type"] == "Aggregate":
            yield plan
        for child in plan.get("Plans", []):
            yield from aggregates(child)

    fairness_plans = [item for item in plans if "run_last_admission AS MATERIALIZED" in item["query"]]
    assert fairness_plans
    for item in fairness_plans:
        fairness = list(aggregates(item["plan"]["Plan"]))
        assert fairness and all(plan["Actual Loops"] <= 1 for plan in fairness)


def test_reference_identifier_generic_plan_uses_unique_expression_index(world):
    table = tables["reference"]
    world.conn.execute(insert(table), [{"project_id": world.project["id"], "cite_key": f"entry-{index}",
        "kind": "article", "title": f"Article {index}", "identifiers": {"doi": f"10.1000/item-{index}"}}
        for index in range(1000)])
    world.conn.execute(text("ANALYZE reference"))
    world.conn.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
    query = select(table).where(table.c.project_id == world.project["id"],
                               identifier_expression(table, "doi") == "10.1000/item-999")
    compiled = query.compile(dialect=world.conn.dialect)
    parameters = list(compiled.params)
    statement = re.sub(r"%\(([^)]+)\)s", lambda match: "$" + str(parameters.index(match[1]) + 1), str(compiled))
    name = "reference_plan_" + uuid4().hex
    world.conn.exec_driver_sql(f"PREPARE {name} AS " + statement)
    try:
        values = {key: str(value) for key, value in compiled.params.items()}
        # Inputs are generated fixture identifiers, never external SQL text.
        arguments = ", ".join("'" + values[key] + "'" for key in parameters)
        plan = world.conn.exec_driver_sql(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) EXECUTE {name} ({arguments})").scalar_one()[0]
        assert "uq_reference_doi" in json.dumps(plan)
        assert world.conn.execute(query).mappings().one()["cite_key"] == "entry-999"
    finally:
        world.conn.exec_driver_sql(f"DEALLOCATE {name}")


def test_shared_host_admits_the_run_without_a_previous_claim_first(world):
    first_run, second_run = world.run(), world.run()
    for run in (first_run, second_run):
        world.disable_automations(run)
        world.assignment(run)
        world.assignment(run)
    first = world.claim()
    second = world.claim()
    assert first and second
    assert get(world.conn, "assignment", first["assignment_id"])["run_id"] != get(
        world.conn, "assignment", second["assignment_id"])["run_id"]
