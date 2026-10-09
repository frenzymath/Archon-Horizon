"""Template provenance, bounded substitution and retained-context regressions."""

import base64
import hashlib
import json
from uuid import UUID

import pytest

from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.instructions import prompts
from archon_horizon.pipeline.instructions.templates import template
from archon_horizon.pipeline.persistence.records import change, create, get
from test_pipeline_service import service_database, world  # noqa: F401
from test_pipeline_reviewer_invocations import review  # noqa: F401


def replace_template(bundle, name, text):
    """Simulate an older pinned artifact without changing installed source."""
    content = text.encode()
    bundle["files"]["prompts/" + name + ".md"] = {
        "content_base64": base64.b64encode(content).decode(),
        "sha256": hashlib.sha256(content).hexdigest(), "executable": False}


def test_template_interpolation_is_literal_and_single_pass():
    bundle = {"prompt_index": [], "files": {}}
    replace_template(bundle, "example", "Hello {{value}}. JSON: {\"a\":1}; $TMPDIR")
    assert template("example", bundle, value="{{other}}") == 'Hello {{other}}. JSON: {"a":1}; $TMPDIR'
    with pytest.raises(ValueError, match="fields differ"):
        template("example", bundle)
    with pytest.raises(ValueError, match="invalid prompt"):
        template("../../secret")


def test_new_bundles_fail_closed_while_legacy_bundles_keep_compatibility():
    bundle = prompts.catalog()
    expected = template("session-handoff", bundle)
    del bundle["files"]["prompts/session-handoff.md"]
    with pytest.raises(DomainError, match="Pinned catalog"):
        template("session-handoff", bundle)
    bundle.pop("prompt_index")
    assert template("session-handoff", bundle) == expected
    replace_template(bundle, "session-handoff", "Changed")
    bundle["prompt_index"] = []
    bundle["files"]["prompts/session-handoff.md"]["sha256"] = "0" * 64
    with pytest.raises(DomainError, match="Invalid pinned prompt"):
        template("session-handoff", bundle)


def test_retained_phase_handoff_and_recovery_use_original_bundle(world, monkeypatch):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run, functions=["planner"])
    bundle = prompts.catalog()
    bundle["phase_contracts"]["preprocessing"] = "PINNED PHASE OUTCOME"
    replace_template(bundle, "session-handoff", "PINNED HANDOFF")
    replace_template(bundle, "planner-recovery", "PINNED RECOVERY")
    monkeypatch.setattr(prompts, "catalog", lambda *args: bundle)
    world.claim()
    prompt = prompts.goal(world.conn, world.service, assignment, run, world.mission, initial=False)
    assert all(value in prompt for value in ("PINNED PHASE OUTCOME", "PINNED HANDOFF", "PINNED RECOVERY"))
    assert prompts.SESSION_HANDOFF not in prompt


def test_old_catalog_keeps_pinned_core_and_function_fields(world, monkeypatch):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run, functions=["planner"])
    bundle = prompts.catalog()
    bundle.pop("prompt_index")
    bundle.pop("phase_contracts")
    bundle["core"] = "OLD PINNED CORE"
    bundle["functions"]["planner"] = "OLD PINNED PLANNER"
    bundle["files"] = {key: value for key, value in bundle["files"].items() if not key.startswith("prompts/")}
    monkeypatch.setattr(prompts, "catalog", lambda *args: bundle)
    world.claim()
    prompt = prompts.goal(world.conn, world.service, assignment, run, world.mission)
    assert "OLD PINNED CORE" in prompt and "OLD PINNED PLANNER" in prompt
    assert prompts.SESSION_HANDOFF in prompt


def test_objective_continuation_has_one_mission_and_bounded_markdown_ledger(world):
    from test_pipeline_objectives import launch
    run = launch(world)
    claim = world.claim()
    assignment = get(world.conn, "assignment", UUID(claim["assignment_id"]))
    mission = get(world.conn, "mission", assignment["mission_id"])
    assignment = change(world.conn, "assignment", assignment["id"], instructions=mission["objective"])
    for number in range(2, prompts.LEDGER_PROMPT_ROWS + 5):
        create(world.conn, "obligation", assignment_id=assignment["id"], number=number,
            description="Detailed obligation " + "é" * 2000)
    prompt = prompts.goal(world.conn, world.service, assignment, run, mission, initial=False)
    assert prompt.count(mission["objective"]) == 1
    assert prompts.OBJECTIVE_CORE not in prompt and prompts.OBJECTIVE_PLANNER not in prompt
    ledger = prompt.split("Open goal ledger:\n", 1)[1].split("\n\n", 1)[0]
    assert len(ledger) <= prompts.LEDGER_PROMPT_CHARS
    assert "Ledger excerpt" in prompt


def test_legacy_ledger_budget_counts_json_escaping_and_metadata(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    for number in range(2, prompts.LEDGER_PROMPT_ROWS + 1):
        create(world.conn, "obligation", assignment_id=assignment["id"], number=number,
               description="é" * 2000)
    prompt = prompts.goal(world.conn, world.service, assignment, run, world.mission, initial=False)
    ledger_text = prompt.split("Current obligations:\n", 1)[1].split("\n\n", 1)[0]
    assert len(ledger_text) <= prompts.LEDGER_PROMPT_CHARS
    assert json.loads(ledger_text)
    assert "Some record text is excerpted" in prompt


@pytest.fixture
def historical_review_catalog(monkeypatch):
    bundle = prompts.catalog()
    source = base64.b64decode(bundle["files"]["prompts/reviewer/start.md"]["content_base64"]).decode()
    replace_template(bundle, "reviewer/start", "PINNED REVIEW START\n" + source)
    replace_template(bundle, "reviewer/packet-snapshot-limits", "PINNED PACKET LIMITS")
    monkeypatch.setattr(prompts, "catalog", lambda *args: bundle)
    return bundle


@pytest.fixture
def historical_review(historical_review_catalog, review):
    # The parent is born with this catalog, preserving database immutability.
    return review


def test_prepared_reviewer_uses_parents_pinned_template_and_packet_guidance(world, historical_review):
    from archon_horizon.pipeline.review.invocations import prepare
    result = prepare(world.conn, historical_review["actor"], historical_review["data"], world.service)
    assert result["prompt"].startswith("PINNED REVIEW START")
    assert "PINNED PACKET LIMITS" in result["prompt"]
