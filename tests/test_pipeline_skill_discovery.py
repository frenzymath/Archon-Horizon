import base64

import pytest

from archon_horizon.pipeline.bundles import skill_files, skill_path
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.worker.skills import materialize_bundle
from test_pipeline_worker_skills import digest


def test_discovery_tracks_operator_override_and_materializes_index(tmp_path):
    source = tmp_path / "source"
    skill = source / "operations" / "horizon-pipeline"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: horizon-pipeline\ndescription: >\n  Custom operational instructions\n  for this installation.\nmetadata:\n  category: operations\n---\n\nPrivate body only read on demand.\n")
    bundle = skill_files(source)
    item = next(item for item in bundle["skill_index"] if item["name"] == "horizon-pipeline")
    assert item["description"] == "Custom operational instructions for this installation."
    overview = base64.b64decode(bundle["files"]["SKILLS.md"]["content_base64"]).decode()
    assert item["description"] in overview
    assert "Private body" not in overview
    state = tmp_path / "state"
    state.mkdir()
    destination = materialize_bundle(state, digest(bundle), bundle)
    assert (destination / "SKILLS.md").read_text() == overview
    assert (destination / item["path"]).read_text().endswith("Private body only read on demand.\n")


def test_operator_skill_without_frontmatter_remains_discoverable(tmp_path):
    skill = tmp_path / "project-specialist"
    skill.mkdir()
    (skill / "SKILL.md").write_text("Local project guidance without frontmatter")
    item = next(item for item in skill_files(tmp_path)["skill_index"] if item["name"] == "project-specialist")
    assert item["category"] == "custom"


def test_specialist_override_is_pinned_and_discovered_without_loading_body(tmp_path):
    source = tmp_path / "source"
    path = source / "subagents" / "research" / "source-researcher.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: source-researcher\ndescription: Local source expert\nskills: [source-research]\n---\nPrivate specialist body.\n")
    bundle = skill_files(source)
    index = base64.b64decode(bundle["files"]["SUBAGENTS.md"]["content_base64"]).decode()
    assert "Local source expert" in index
    assert "Private specialist body" not in index
    assert all(not item.startswith("subagents/") for item in bundle["entrypoints"])
    state = tmp_path / "state"
    state.mkdir()
    destination = materialize_bundle(state, digest(bundle), bundle)
    assert (destination / "SUBAGENTS.md").read_text() == index
    assert (destination / path.relative_to(source)).read_text() == path.read_text()


@pytest.mark.parametrize("header, code", [
    ("name: wrong\ndescription: Invalid\nskills: []", "invalid_subagent_metadata"),
    ("name: specialist\ndescription: Unknown skill\nskills: [missing-skill]", "unknown_subagent_skill"),
    ("name: specialist\ndescription: Invalid\nskills: false", "invalid_subagent_metadata"),
])
def test_broken_specialist_fails_before_dispatch(tmp_path, header, code):
    path = tmp_path / "subagents" / "research" / "specialist.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\n{header}\n---\nBody\n")
    with pytest.raises(DomainError) as error:
        skill_files(tmp_path)
    assert error.value.code == code


def test_entrypoint_uses_pinned_location_and_rejects_ambiguous_names(tmp_path):
    bundle = skill_files()
    assert skill_path(bundle, "horizon-pipeline") == "operations/horizon-pipeline/SKILL.md"
    retained = {"files": {"horizon-pipeline/SKILL.md": {}}, "entrypoints": ["horizon-pipeline/SKILL.md"]}
    assert skill_path(retained, "horizon-pipeline") == "horizon-pipeline/SKILL.md"
    duplicate = tmp_path / "other" / "horizon-pipeline"
    duplicate.mkdir(parents=True)
    (duplicate / "SKILL.md").write_text("---\nname: horizon-pipeline\n---\nDuplicate")
    with pytest.raises(DomainError) as error:
        skill_files(tmp_path)
    assert error.value.code == "duplicate_skill_name"


@pytest.mark.parametrize("header", ["name: [bad]", "description: false", "[broken", "- not-a-map"])
def test_invalid_metadata_fails_before_pinning(tmp_path, header):
    skill = tmp_path / "invalid"
    skill.mkdir()
    (skill / "SKILL.md").write_text(f"---\n{header}\n---\nBody\n")
    with pytest.raises(DomainError) as error:
        skill_files(tmp_path)
    assert error.value.code == "invalid_skill_metadata"
