"""Generate the static dashboard (a single self-contained HTML page).

Per the roadmap the static dashboard *shows* state and never persists edits:
the local inbox is editable through the CLI/live dashboard, and the GitHub
shadow is read-only here. Math is rendered client-side with KaTeX (CDN), the
same renderer the blueprint view uses — so this stays a pure view layer.

The function takes already-assembled data (it does no I/O), so it is trivial
to test and the caller decides where to write the page.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape
from typing import Any

from archon_horizon.core.inbox import InboxItem
from archon_horizon.core.roadmap import Roadmap

_KATEX = (
    '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16/dist/katex.min.css">'
    '<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16/dist/katex.min.js"></script>'
    '<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16/dist/contrib/auto-render.min.js"'
    ' onload="renderMathInElement(document.body)"></script>'
)


def _roadmap_section(roadmap: Roadmap) -> str:
    if not roadmap.items:
        return "<p><em>No roadmap items.</em></p>"
    rows = "".join(
        f"<tr><td>{escape(i.id)}</td><td>{escape(i.title)}</td>"
        f"<td>{escape(i.status)}</td><td>{escape(i.kind)}</td>"
        f"<td>{escape(', '.join(i.projects))}</td>"
        f"<td>{escape(', '.join(i.depends_on))}</td></tr>"
        for i in roadmap.items
    )
    return (
        "<table><thead><tr><th>ID</th><th>Title</th><th>Status</th>"
        f"<th>Kind</th><th>Projects</th><th>Depends on</th></tr></thead><tbody>{rows}</tbody></table>"
    )


def _inbox_section(items: Sequence[InboxItem], *, editable: bool) -> str:
    note = (
        "<p class='note'>Editable via <code>archon-horizon inbox …</code>.</p>"
        if editable
        else "<p class='note'>Read-only shadow — change labels/comments in GitHub.</p>"
    )
    if not items:
        return note + "<p><em>Empty.</em></p>"
    rows = "".join(
        f"<tr><td>{escape(i.id)}</td><td>{escape(i.kind)}</td>"
        f"<td>{escape(i.status)}</td><td>{escape(', '.join(i.labels))}</td>"
        f"<td>{escape(i.body)}</td></tr>"
        for i in items
    )
    return note + (
        "<table><thead><tr><th>ID</th><th>Kind</th><th>Status</th>"
        f"<th>Labels</th><th>Body</th></tr></thead><tbody>{rows}</tbody></table>"
    )


def _dag_section(dag: dict[str, Any] | None) -> str:
    if not dag:
        return "<p><em>No blueprint DAG.</em></p>"
    nodes = dag.get("nodes", [])
    edges = dag.get("edges", [])
    items = "".join(
        f"<li><strong>{escape(str(n.get('id')))}</strong> "
        f"({escape(str(n.get('kind', '')))}){' ✓' if n.get('leanok') else ''}</li>"
        for n in nodes
    )
    return (
        f"<p>{len(nodes)} nodes, {len(edges)} edges. "
        "Full graph data in <code>dag.json</code>.</p>"
        f"<ul>{items}</ul>"
    )


def render_dashboard(
    *,
    workspace_name: str,
    roadmap: Roadmap,
    local_items: Sequence[InboxItem] = (),
    github_items: Sequence[InboxItem] = (),
    memory: str = "",
    reports: Sequence[str] = (),
    dag: dict[str, Any] | None = None,
    live: bool = False,
) -> str:
    report_links = "".join(
        f"<li><a href='reports/{escape(r)}.md'>{escape(r)}</a></li>" for r in reports
    ) or "<li><em>none</em></li>"
    refresh = '<meta http-equiv="refresh" content="5">' if live else ""
    banner = (
        "Live dashboard — auto-refreshing. Edit the local inbox via the server API or CLI."
        if live
        else "Static dashboard — generated, never persists edits."
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">{refresh}
<title>{escape(workspace_name)} — Archon Horizon</title>{_KATEX}
<style>
 body {{ font: 15px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 60rem; color: #1a1a1a; }}
 h1 {{ border-bottom: 2px solid #333; }}
 table {{ border-collapse: collapse; width: 100%; margin: .5rem 0; }}
 th, td {{ border: 1px solid #ddd; padding: .35rem .5rem; text-align: left; vertical-align: top; }}
 th {{ background: #f4f4f4; }}
 .note {{ color: #666; font-size: .85em; }}
 pre {{ background: #f6f8fa; padding: 1rem; overflow-x: auto; }}
</style></head><body>
<h1>{escape(workspace_name)}</h1>
<p class="note">{banner}</p>
<h2>Roadmap</h2>{_roadmap_section(roadmap)}
<h2>Blueprint DAG</h2>{_dag_section(dag)}
<h2>Local inbox</h2>{_inbox_section(local_items, editable=True)}
<h2>GitHub inbox (shadow)</h2>{_inbox_section(github_items, editable=False)}
<h2>Memory</h2><pre>{escape(memory) or '<em>empty</em>'}</pre>
<h2>Reports</h2><ul>{report_links}</ul>
</body></html>
"""
