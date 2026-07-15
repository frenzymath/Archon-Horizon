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
from archon_horizon.core.roadmap import Roadmap, ordered_tree

_KATEX = (
    '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16/dist/katex.min.css">'
    '<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16/dist/katex.min.js"></script>'
    '<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16/dist/contrib/auto-render.min.js"'
    ' onload="renderMathInElement(document.body)"></script>'
)


def _roadmap_section(roadmap: Roadmap) -> str:
    if not roadmap.items:
        return "<p class='empty-state'>No roadmap items.</p>"
    # Render as an outline: parents above their sub-items, indented by tree depth.
    rows = "".join(
        f"<tr><td style='padding-left:{depth * 1.4:.1f}em'>"
        f"<span class='badge'>{escape(i.id)}</span></td>"
        f"<td style='padding-left:{depth * 1.4:.1f}em'><strong>{escape(i.title)}</strong></td>"
        f"<td><span class='badge status-{escape(i.status)}'>{escape(i.status)}</span></td>"
        f"<td>{escape(i.kind)}</td>"
        f"<td><span class='badge'>{escape(', '.join(i.projects))}</span></td>"
        f"<td>{escape(', '.join(i.depends_on))}</td></tr>"
        for i, depth in ordered_tree(roadmap.items)
    )
    return (
        "<div class='table-container'><table><thead><tr><th>ID</th><th>Title</th><th>Status</th>"
        f"<th>Kind</th><th>Projects</th><th>Depends on</th></tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _inbox_section(items: Sequence[InboxItem], *, editable: bool) -> str:
    note = (
        "<p class='note'>Editable via <code>archon-horizon inbox …</code>.</p>"
        if editable
        else "<p class='note'>Read-only shadow — change labels/comments in GitHub.</p>"
    )
    if not items:
        return note + "<p class='empty-state'>Empty.</p>"
    rows = "".join(
        f"<tr><td><span class='badge'>{escape(i.id)}</span></td>"
        f"<td>{escape(i.kind)}</td>"
        f"<td><span class='badge status-{escape(i.status)}'>{escape(i.status)}</span></td>"
        f"<td>{escape(', '.join(i.labels))}</td>"
        f"<td>{escape(i.body)}</td></tr>"
        for i in items
    )
    return note + (
        "<div class='table-container'><table><thead><tr><th>ID</th><th>Kind</th><th>Status</th>"
        f"<th>Labels</th><th>Body</th></tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _dag_section(dag: dict[str, Any] | None) -> str:
    if not dag:
        return "<p class='empty-state'>No blueprint DAG.</p>"
    nodes = dag.get("nodes", [])
    edges = dag.get("edges", [])
    # The ✓ span is built outside the f-string: it contains double quotes, which
    # can't be escaped inside an f-string expression on Python 3.11 (a backslash
    # in an f-expression is a SyntaxError there).
    tick = ' <span class="text-emerald-400">✓</span>'
    items = "".join(
        f"<li><span class='badge'>{escape(str(n.get('id')))}</span> "
        f"({escape(str(n.get('kind', '')))}){tick if n.get('leanok') else ''}</li>"
        for n in nodes
    )
    return (
        f"<p class='note'>{len(nodes)} nodes, {len(edges)} edges. "
        "Full graph data in <code>dag.json</code>.</p>"
        f"<ul class='dag-list'>{items}</ul>"
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
        f"<li><a href='reports/{escape(r)}.md' class='report-link'>{escape(r)}</a></li>" for r in reports
    ) or "<li><em class='empty-state'>none</em></li>"
    refresh = '<meta http-equiv="refresh" content="5">' if live else ""
    banner = (
        "Live dashboard — auto-refreshing. Edit the local inbox via the server API or CLI."
        if live
        else "Static dashboard — generated, never persists edits."
    )
    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">{refresh}
    <title>{escape(workspace_name)} — Archon Horizon</title>
    {_KATEX}
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-base: #0f172a;
            --bg-panel: rgba(30, 41, 59, 0.7);
            --border: #334155;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --accent: #38bdf8;
            --accent-hover: #0ea5e9;
        }}
        body {{
            font-family: 'Inter', sans-serif;
            background: var(--bg-base);
            color: var(--text-main);
            margin: 0;
            padding: 0;
            min-height: 100vh;
            background-image: radial-gradient(circle at 50% 0%, #1e293b 0%, transparent 70%);
        }}
        .header {{
            padding: 2rem;
            text-align: center;
            border-bottom: 1px solid var(--border);
            background: rgba(15, 23, 42, 0.8);
            backdrop-filter: blur(10px);
            position: sticky;
            top: 0;
            z-index: 100;
        }}
        h1 {{ margin: 0; font-weight: 700; letter-spacing: -0.02em; background: linear-gradient(to right, #38bdf8, #818cf8); -webkit-background-clip: text; color: transparent; }}
        .header-note {{ margin-top: 0.5rem; color: var(--text-muted); font-size: 0.9rem; }}
        .nav-tabs {{
            display: flex;
            justify-content: center;
            gap: 1rem;
            margin-top: 1.5rem;
        }}
        .tab-btn {{
            background: transparent;
            color: var(--text-muted);
            border: none;
            padding: 0.5rem 1rem;
            font-size: 1rem;
            font-weight: 500;
            cursor: pointer;
            border-bottom: 2px solid transparent;
            transition: all 0.2s;
        }}
        .tab-btn:hover {{ color: var(--text-main); }}
        .tab-btn.active {{ color: var(--accent); border-bottom-color: var(--accent); }}
        
        .container {{
            max-width: 1200px;
            margin: 2rem auto;
            padding: 0 2rem;
        }}
        .tab-content {{
            display: none;
            animation: fadeIn 0.3s ease-in-out;
        }}
        .tab-content.active {{ display: block; }}
        
        .panel {{
            background: var(--bg-panel);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 1.5rem;
            margin-bottom: 2rem;
            backdrop-filter: blur(12px);
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
        }}
        .panel h2 {{ margin-top: 0; font-size: 1.25rem; font-weight: 600; border-bottom: 1px solid var(--border); padding-bottom: 0.5rem; margin-bottom: 1rem; }}
        
        .table-container {{ overflow-x: auto; }}
        table {{ width: 100%; border-collapse: collapse; text-align: left; }}
        th, td {{ padding: 0.75rem 1rem; border-bottom: 1px solid var(--border); }}
        th {{ color: var(--text-muted); font-weight: 500; font-size: 0.875rem; text-transform: uppercase; letter-spacing: 0.05em; }}
        tr:hover td {{ background: rgba(255,255,255,0.02); }}
        
        .badge {{ background: #1e293b; border: 1px solid var(--border); padding: 0.125rem 0.375rem; border-radius: 4px; font-size: 0.75rem; font-family: monospace; }}
        .status-completed {{ color: #10b981; border-color: rgba(16,185,129,0.3); background: rgba(16,185,129,0.1); }}
        .status-running {{ color: #38bdf8; border-color: rgba(56,189,248,0.3); background: rgba(56,189,248,0.1); }}
        .status-failed {{ color: #ef4444; border-color: rgba(239,68,68,0.3); background: rgba(239,68,68,0.1); }}
        
        pre {{ background: #0b1120; border: 1px solid var(--border); padding: 1rem; border-radius: 8px; overflow-x: auto; color: #e2e8f0; font-family: monospace; font-size: 0.875rem; }}
        .note {{ color: var(--text-muted); font-size: 0.875rem; margin-bottom: 1rem; }}
        .empty-state {{ color: var(--text-muted); font-style: italic; }}
        .dag-list {{ list-style: none; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 0.5rem; }}
        .dag-list li {{ background: rgba(255,255,255,0.03); border: 1px solid var(--border); padding: 0.5rem; border-radius: 6px; font-size: 0.875rem; }}
        .report-link {{ color: var(--accent); text-decoration: none; transition: color 0.2s; }}
        .report-link:hover {{ color: var(--accent-hover); text-decoration: underline; }}
        
        @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(5px); }} to {{ opacity: 1; transform: translateY(0); }} }}
    </style>
</head>
<body>
    <div class="header">
        <h1>{escape(workspace_name)}</h1>
        <div class="header-note">{banner}</div>
        <div class="nav-tabs">
            <button class="tab-btn active" onclick="showTab(event, 'tab-roadmap')">Roadmap</button>
            <button class="tab-btn" onclick="showTab(event, 'tab-inboxes')">Inboxes</button>
            <button class="tab-btn" onclick="showTab(event, 'tab-blueprint')">Blueprint DAG</button>
            <button class="tab-btn" onclick="showTab(event, 'tab-memory')">Memory</button>
            <button class="tab-btn" onclick="showTab(event, 'tab-reports')">Reports</button>
        </div>
    </div>
    
    <div class="container">
        <div id="tab-roadmap" class="tab-content active">
            <div class="panel">
                <h2>Roadmap</h2>
                {_roadmap_section(roadmap)}
            </div>
        </div>
        
        <div id="tab-inboxes" class="tab-content">
            <div class="panel">
                <h2>Local Inbox</h2>
                {_inbox_section(local_items, editable=True)}
            </div>
            <div class="panel">
                <h2>GitHub Inbox (Shadow)</h2>
                {_inbox_section(github_items, editable=False)}
            </div>
        </div>
        
        <div id="tab-blueprint" class="tab-content">
            <div class="panel">
                <h2>Blueprint DAG</h2>
                {_dag_section(dag)}
            </div>
        </div>
        
        <div id="tab-memory" class="tab-content">
            <div class="panel">
                <h2>Memory</h2>
                <pre>{escape(memory) or '<em class="empty-state">Empty.</em>'}</pre>
            </div>
        </div>
        
        <div id="tab-reports" class="tab-content">
            <div class="panel">
                <h2>Reports</h2>
                <ul style="list-style: none; padding: 0; line-height: 1.8;">{report_links}</ul>
            </div>
        </div>
    </div>
    
    <script>
        function showTab(event, id) {{
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
            document.getElementById(id).classList.add('active');
            event.currentTarget.classList.add('active');
        }}
    </script>
</body>
</html>
"""
