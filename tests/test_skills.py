"""Bundled capability skills: enumeration, frontmatter, and installation."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.skills.registry import available_skills, install_skills

EXPECTED = {
    "horizon",
    "horizon-inbox",
    "lean-check",
    "hgraph",
    "blueprint-conventions",
    "project-git",
    "formalization-review",
    "honesty",
    "review-method",
    "progress-integrity",
    "source-fidelity",
    "semantic-adversarial",
    "blueprint-integrity",
    "verification-evidence",
    "api-composition",
    "lean-quality",
    "mathlib-orientation",
    "consumer-dependency",
    "load-bearing",
    "graph-traceability",
    "provenance-isolation",
    "strategy-convergence",
    "run-health",
    "external-boundary",
    "transcription-fidelity",
    "release-reproducibility",
    "review-adjudication",
    "source-discovery",
    "mathlib-conventions",
    "leansearch",
    "horizon-start",
    "horizon-waiting",
    "task-status",
    "references",
    "subagents",
    "restart-module",
    "definition-quality",
}


def test_available_skills_have_names_and_descriptions() -> None:
    skills = available_skills()
    names = {s.name for s in skills}
    assert EXPECTED <= names
    assert all(s.description for s in skills)  # every skill documents its purpose


def test_skill_recommendations_are_advisory_metadata() -> None:
    skills = {skill.name: skill for skill in available_skills()}
    recommendation = skills["lean-check"].recommendation
    assert recommendation
    assert "in-place" in recommendation
    assert "by sorry" in recommendation
    assert "consider" in recommendation

    review_recommendation = skills["formalization-review"].recommendation
    assert "consider" in review_recommendation
    assert "not a completion gate" in review_recommendation

    honesty = skills["honesty"]
    assert "certificate" in honesty.description
    assert "consider" in honesty.recommendation
    assert "not a proof gate" in honesty.recommendation

    discovery = skills["source-discovery"]
    assert "GitHub" in discovery.description
    assert "Zulip" in discovery.description
    assert "consider" in discovery.recommendation


def test_install_skills_writes_claude_skill_files(tmp_path: Path) -> None:
    installed = install_skills(tmp_path)
    assert EXPECTED <= set(installed)
    for name in EXPECTED:
        skill_file = tmp_path / ".claude" / "skills" / name / "SKILL.md"
        assert skill_file.exists()
        assert skill_file.read_text("utf-8").startswith("---")  # frontmatter preserved
