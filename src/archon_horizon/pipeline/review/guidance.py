"""Packaged reviewer perspectives shared by bootstrap and instruction catalogs."""

from __future__ import annotations

from .._resources import SKILLS_ROOT, SUBAGENTS_ROOT
from ..instructions.subagent_guidance import descriptor as parse_descriptor

ROOT = SUBAGENTS_ROOT / "reviewers"
CONTRACT = SKILLS_ROOT / "review" / "horizon-review" / "references" / "review-contract.md"
PERSPECTIVES = (
    "statement-fidelity", "decomposition", "definitions", "roadmap-consistency",
    "mathematical-fidelity", "library-api", "library-architecture", "lean-proof-quality",
    "lean-performance", "repository-quality", "scholarly-quality",
)


def descriptor(name: str) -> dict:
    if name not in PERSPECTIVES:
        raise ValueError(f"Unknown reviewer perspective: {name}")
    source = ROOT.joinpath(f"{name}.md").read_text()
    result = parse_descriptor(f"subagents/reviewers/{name}.md", source, purpose="review")
    result["functions"] = ["reviewer"]
    result["instructions"] = CONTRACT.read_text().strip() + "\n\n" + result["instructions"]
    return result


def instructions(name: str) -> str:
    return descriptor(name)["instructions"]


def descriptors() -> list[dict]:
    return [descriptor(name) for name in PERSPECTIVES]
