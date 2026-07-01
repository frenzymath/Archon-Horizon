"""Turn harness output into an :class:`GroundUpdate`.

There is no machine-readable contract anymore: the Ground agent acts during
its run (editing roadmap/memory/blueprints on disk, mutating the inbox via the
``horizon`` CLI) and ends with a human report. So parsing is trivial — the whole
response *is* the report. The function is kept as the harness's injectable
``parse`` callable so an engine could still post-process output if needed.
"""

from __future__ import annotations

from .base import GroundUpdate


def _clean_report(text: str) -> str:
    report = text.strip()
    if not report:
        return report

    # Some command-line agents stream working narration and then repeat the
    # final report. Keep the last explicit report block when one is present.
    markers = ("## Run-local report", "# Run-local report", "Run-local report")
    for marker in markers:
        idx = report.rfind(marker)
        if idx > 0:
            report = report[idx:].strip()
            break

    lead_ins = (
        "Done. Here's the run-local report.",
        "Done. Here is the run-local report.",
    )
    lines = report.splitlines()
    while lines and lines[0].strip() in lead_ins:
        lines.pop(0)
    return "\n".join(lines).strip()


def parse_ground_update(text: str) -> GroundUpdate:
    return GroundUpdate(report=_clean_report(text))
