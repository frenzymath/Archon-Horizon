"""Shipped review presets must be executable with the pinned skill catalog."""

from __future__ import annotations

import base64
from uuid import uuid4

import pytest

from archon_horizon.pipeline.projects.bootstrap import PRESETS, ProjectRecipe, expanded
from archon_horizon.pipeline.models import ReviewerDescriptorCreate
from archon_horizon.pipeline.instructions.prompts import catalog
from archon_horizon.pipeline.instructions.instruction_catalog import read_catalog
from archon_horizon.pipeline.review.guidance import PERSPECTIVES, instructions


def test_every_phase_preset_uses_available_functions_and_packaged_review_guidance():
    pinned = catalog()
    project_id = uuid4()
    recipe = ProjectRecipe(id=uuid4(), records=[{
        "key": "project", "kind": "project", "values": {"slug": "review-fixture", "title": "Review fixture"},
    }], review_presets=[{
        "key": name, "preset": name, "project_id": project_id, "repository_id": uuid4(),
    } for name in PRESETS])
    descriptors = [row for row in expanded(recipe) if row.kind == "reviewer_descriptor"]
    assert descriptors
    catalog_instructions = {row["instructions"] for row in pinned["reviewers"]}
    for record in descriptors:
        parsed = ReviewerDescriptorCreate.model_validate(record.values)
        assert parsed.invocation == "assignment"
        # Preparation rejects unavailable functions before launching any reviewer.
        assert set(parsed.functions) <= pinned["functions"].keys()
        assert parsed.instructions in catalog_instructions
    assert "review/horizon-review/SKILL.md" in pinned["entrypoints"]
    assert "review/horizon-review/references/forge-reviews.md" in pinned["files"]
    assert "subagents/reviewers/library-api.md" in pinned["files"]
    assert all(not path.startswith("subagents/") for path in pinned["entrypoints"])


def test_review_presets_allow_explicit_native_mode():
    recipe = ProjectRecipe(id=uuid4(), records=[{
        "key": "project", "kind": "project", "values": {"slug": "native-review", "title": "Native review"},
    }], review_presets=[{
        "key": "reviews", "preset": "preprocessing-roadmap", "project_id": uuid4(),
        "repository_id": uuid4(), "invocation": "subrequest",
    }])
    descriptors = [row for row in expanded(recipe) if row.kind == "reviewer_descriptor"]
    assert descriptors and all(row.values["invocation"] == "subrequest" for row in descriptors)


def test_unknown_reviewer_name_cannot_read_an_arbitrary_file():
    with pytest.raises(ValueError, match="Unknown reviewer"):
        instructions("../../prompts")


def test_every_shipped_reviewer_uses_the_same_bundled_contract_and_preview():
    pinned = catalog()
    contract = base64.b64decode(pinned["files"][
        "review/horizon-review/references/review-contract.md"]["content_base64"]).decode().strip()
    assert {row["slug"] for row in pinned["reviewers"]} == set(PERSPECTIVES)
    for row in pinned["reviewers"]:
        assert row["instructions"].startswith(contract + "\n\n")
        preview = read_catalog(path=row["source_path"])
        assert preview["instructions"] == row["instructions"]


def test_library_audit_capabilities_are_discoverable_without_new_execution_roles():
    pinned = catalog()
    skills = {row["name"] for row in pinned["skill_index"]}
    assert {"library-audit", "horizon-communication"} <= skills
    auditor = next(row for row in pinned["subagent_index"] if row["slug"] == "library-auditor")
    assert auditor["category"] == "validation"
    assert "library-audit" in auditor["skills"]
    assert "library-architecture" in PRESETS["postprocessing-library"]["reviewers"]
    assert "library-auditor" not in pinned["functions"]
    assert "library-architecture" not in pinned["functions"]
    assert "lean/lean-performance/scripts/compare_measurements.py" in pinned["files"]


def test_audit_helper_is_discoverable_without_a_standing_execution_role():
    pinned = catalog()
    auditor = next(row for row in pinned["subagent_index"] if row["slug"] == "orchestration-auditor")
    assert auditor["category"] == "validation"
    assert auditor["skills"] == ["horizon-operations"]
    assert auditor["source_path"] in pinned["files"]
    skill = next(row for row in pinned["skill_index"] if row["name"] == "horizon-operations")
    assert skill["path"] in pinned["files"]
    assert "orchestration-auditor" not in pinned["functions"]
