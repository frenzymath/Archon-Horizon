"""Bundled capability skills: enumeration, frontmatter, and installation."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.skills.registry import available_skills, install_skills

EXPECTED = {"horizon", "horizon-inbox", "lean-check", "hgraph", "blueprint-conventions", "project-git"}


def test_available_skills_have_names_and_descriptions() -> None:
    skills = available_skills()
    names = {s.name for s in skills}
    assert EXPECTED <= names
    assert all(s.description for s in skills)  # every skill documents its purpose


def test_install_skills_writes_claude_skill_files(tmp_path: Path) -> None:
    installed = install_skills(tmp_path)
    assert EXPECTED <= set(installed)
    for name in EXPECTED:
        skill_file = tmp_path / ".claude" / "skills" / name / "SKILL.md"
        assert skill_file.exists()
        assert skill_file.read_text("utf-8").startswith("---")  # frontmatter preserved
