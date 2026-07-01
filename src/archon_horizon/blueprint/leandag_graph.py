"""Rich dependency graph via leandag (the optional ``dag`` extra).

When ``leandag`` is installed, this produces the same rich per-node data Archon's
DAG page shows — effort, proof sizes, Lean source, dep/rdep counts, sorry/proved
status — by scanning the project's Lean sources together with its blueprint. It
is a drop-in upgrade for the built-in blueprint parser: callers fall back to the
parser when leandag is absent or the project has no blueprint entry.

Ported from Archon's ``commands/dag/leandag_gaps.py`` (``_build_dag`` /
``_serialize_graph`` / ``_detect_entry``). Edges are emitted as ``source/target``
to match :mod:`archon_horizon.blueprint.dag`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_ENTRY_NAMES = ("web.tex", "print.tex", "content.tex")


def leandag_available() -> bool:
    # leandag is vendored under archon_horizon.leandag, so it is always present;
    # the try/except stays as a guard against an import-time error in the vendor.
    try:
        from archon_horizon import leandag  # noqa: F401
    except Exception:
        return False
    return True


def _detect_entry(project_root: Path, blueprint_dir: Path | None) -> Path | None:
    dirs: list[Path] = []
    if blueprint_dir is not None:
        dirs += [blueprint_dir, blueprint_dir / "src"]
    dirs += [project_root / "blueprint" / "src", project_root / "blueprint"]
    for directory in dirs:
        for name in _ENTRY_NAMES:
            candidate = directory / name
            if candidate.exists():
                return candidate
    return None


def build_project_graph(project_root: Path, blueprint_dir: Path | None = None) -> dict[str, Any] | None:
    """Build the rich leandag graph for one project, or None to fall back.

    Returns ``{"nodes", "edges", "dangling", "meta"}`` with edges as
    ``source/target``. None when leandag is missing, there is no blueprint
    entry, or leandag raises.
    """
    try:
        from archon_horizon.leandag import DAG, BlueprintParser, LeanScanner
    except Exception:
        return None
    entry = _detect_entry(project_root, blueprint_dir)
    if entry is None:
        return None
    try:
        lean_decls = LeanScanner().scan(project_root)
        parser = BlueprintParser(entry)
        bp_decls, proofs = parser.parse()
        macros = getattr(parser, "macros", {}) or {}
        dag = DAG.from_sources(bp_decls, proofs, lean_decls, macros=macros)
    except Exception:
        return None

    seen: set[str] = set()
    nodes: list[dict[str, Any]] = []
    duplicate_ids: list[str] = []
    for node in dag.nodes:
        record = node.to_dict()
        node_id = record.get("id")
        if node_id in seen:
            duplicate_ids.append(node_id)
            continue
        seen.add(node_id)
        nodes.append(record)

    return {
        "nodes": nodes,
        "edges": [{"source": e.source, "target": e.target} for e in dag.edges],
        "dangling": [],  # leandag resolves \uses; key kept for shape-compat
        "meta": {
            "engine": "leandag",
            "entry": str(entry),
            "macros": getattr(dag, "macros", {}) or {},
            "duplicate_ids": sorted(set(duplicate_ids)),
        },
    }
