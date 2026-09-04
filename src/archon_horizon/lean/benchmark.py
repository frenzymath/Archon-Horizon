"""Static Lean cost signals from ``set_option`` heartbeat overrides.

The simple indicator is the sum of numeric heartbeat budgets introduced by
``set_option maxHeartbeats …`` and ``set_option synthInstance.maxHeartbeats …``
(and related resource options). High totals flag files that may need redesign
or a clean rewrite rather than local patching.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

# Directories never treated as authored Lean (mirrors server/source_api).
_SKIP_DIRS = {
    ".lake",
    "_lake",
    ".archon",
    ".archon-horizon",
    ".git",
    "node_modules",
    "lake-packages",
}

# Resource knobs that agents raise when elaboration/synthesis is stuck.
# Values are summed as the per-file "benchmark" score.
_HEARTBEAT_OPTIONS = (
    "maxHeartbeats",
    "synthInstance.maxHeartbeats",
    "maxRecDepth",
    "maxSynthPending",
)

_OPTION_ALT = "|".join(re.escape(name) for name in _HEARTBEAT_OPTIONS)
# Matches both file-scoped and `in`-scoped forms, e.g.
#   set_option maxHeartbeats 1000000
#   set_option maxHeartbeats 1_000_000 in
#   set_option synthInstance.maxHeartbeats 400000 in
_SET_OPTION_RE = re.compile(
    rf"(?m)^[ \t]*set_option[ \t]+(?P<option>{_OPTION_ALT})[ \t]+"
    rf"(?P<value>\d[\d_]*)(?:[ \t]+in)?[ \t]*(?:--.*)?$"
)


@dataclass(frozen=True, slots=True)
class HeartbeatHit:
    """One ``set_option`` resource override in a Lean file."""

    line: int
    option: str
    value: int
    text: str


@dataclass(frozen=True, slots=True)
class FileBenchmark:
    """Aggregated heartbeat cost for one Lean file."""

    path: str
    heartbeats: int
    hits: int
    options: tuple[str, ...]
    details: tuple[HeartbeatHit, ...]

    def as_dict(self, *, include_details: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "path": self.path,
            "heartbeats": self.heartbeats,
            "hits": self.hits,
            "options": list(self.options),
        }
        if include_details:
            payload["details"] = [asdict(hit) for hit in self.details]
        return payload


def parse_heartbeat_value(raw: str) -> int:
    """Parse a Lean numeric literal that may use ``_`` digit separators."""
    return int(raw.replace("_", ""))


def scan_lean_text(text: str) -> list[HeartbeatHit]:
    """Return every heartbeat-style ``set_option`` in ``text`` (1-based lines)."""
    hits: list[HeartbeatHit] = []
    for match in _SET_OPTION_RE.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        value = parse_heartbeat_value(match.group("value"))
        hits.append(
            HeartbeatHit(
                line=line,
                option=match.group("option"),
                value=value,
                text=match.group(0).strip(),
            )
        )
    return hits


def benchmark_file(path: Path, *, relative: str | None = None) -> FileBenchmark | None:
    """Scan one ``.lean`` file. Returns ``None`` when the file cannot be read."""
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return None
    hits = scan_lean_text(text)
    total = sum(hit.value for hit in hits)
    options = tuple(sorted({hit.option for hit in hits}))
    return FileBenchmark(
        path=relative if relative is not None else path.as_posix(),
        heartbeats=total,
        hits=len(hits),
        options=options,
        details=tuple(hits),
    )


def iter_lean_files(root: Path) -> Iterable[Path]:
    """Yield authored ``.lean`` files under ``root``, skipping build trees."""
    if not root.is_dir():
        return
    for path in root.rglob("*.lean"):
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        yield path


def benchmark_project(
    project_path: Path,
    *,
    min_heartbeats: int = 1,
    include_details: bool = True,
) -> list[dict[str, object]]:
    """Rank Lean files in a project by total heartbeat budget (descending)."""
    rows: list[FileBenchmark] = []
    for path in iter_lean_files(project_path):
        rel = path.relative_to(project_path).as_posix()
        row = benchmark_file(path, relative=rel)
        if row is None:
            continue
        if row.heartbeats < min_heartbeats and row.hits == 0:
            continue
        if min_heartbeats > 0 and row.heartbeats < min_heartbeats:
            continue
        rows.append(row)
    rows.sort(key=lambda r: (-r.heartbeats, -r.hits, r.path))
    return [r.as_dict(include_details=include_details) for r in rows]


def benchmark_workspace(
    projects: dict[str, Path],
    *,
    min_heartbeats: int = 1,
    include_details: bool = False,
    limit: int | None = None,
) -> dict[str, object]:
    """Cross-project ranking. ``projects`` maps project name → root path."""
    files: list[dict[str, object]] = []
    per_project: dict[str, dict[str, object]] = {}
    for name, root in sorted(projects.items()):
        project_rows = benchmark_project(
            root,
            min_heartbeats=0,  # keep zeros for per-project totals; filter below
            include_details=include_details,
        )
        # Include only files that actually raised a budget when min > 0.
        ranked = [
            row
            for row in project_rows
            if int(row["heartbeats"]) >= min_heartbeats
            and (min_heartbeats == 0 or int(row["hits"]) > 0 or int(row["heartbeats"]) > 0)
        ]
        if min_heartbeats > 0:
            ranked = [row for row in ranked if int(row["hits"]) > 0]
        total_hb = sum(int(r["heartbeats"]) for r in ranked)
        total_hits = sum(int(r["hits"]) for r in ranked)
        per_project[name] = {
            "project": name,
            "files": len(ranked),
            "heartbeats": total_hb,
            "hits": total_hits,
        }
        for row in ranked:
            entry = dict(row)
            entry["project"] = name
            files.append(entry)
    files.sort(
        key=lambda r: (-int(r["heartbeats"]), -int(r["hits"]), str(r["project"]), str(r["path"]))
    )
    if limit is not None and limit >= 0:
        files = files[:limit]
    return {
        "indicator": "sum of set_option heartbeat budgets",
        "options": list(_HEARTBEAT_OPTIONS),
        "min_heartbeats": min_heartbeats,
        "files": files,
        "projects": [per_project[name] for name in sorted(per_project)],
        "total_files": len(files),
        "total_heartbeats": sum(int(f["heartbeats"]) for f in files),
        "total_hits": sum(int(f["hits"]) for f in files),
    }
