"""``horizon search`` — offline Lean declaration search over configured libraries.

Three modes share one cached index:
  • default          BM25 over names/signatures/docstrings (natural language),
  • ``--name``       Loogle-style name match,
  • ``--type``       heuristic type-pattern match (``?a``/``_`` are wildcards).
"""

from __future__ import annotations

import json as _json

import typer

from archon_horizon.log import log

from .shared import load_workspace

app = typer.Typer(help="Search local workspace/configured Lean declarations; use LeanSearch/Loogle/Moogle/LSP too for broad Mathlib search.")


def search(
    ctx: typer.Context,
    query: str = typer.Argument("", help="Natural-language query (or a name/type pattern with --name/--type)."),
    name: str | None = typer.Option(None, "--name", help="Match declaration names (Loogle-style)."),
    type_pattern: str | None = typer.Option(None, "--type", help="Match signatures; '?a'/'_' are wildcards."),
    library: str | None = typer.Option(None, "--lib", help="Restrict to one library/project by name."),
    limit: int = typer.Option(10, "-n", "--limit", help="Max results."),
    reindex: bool = typer.Option(False, "--reindex", help="Force a rebuild of the declaration index."),
    as_json: bool = typer.Option(False, "--json", help="Emit results as JSON."),
) -> None:
    """Find lemmas/definitions in mathlib and the other configured libraries.

    The index is built from the Lean sources already on disk — no GPU, API key,
    or model required — and cached under the workspace state dir. It is best for
    local projects and configured imported libraries. For broad Mathlib precedent
    search, also try LeanSearch/Loogle/Moogle or Lean LSP MCP search tools.
    """
    from archon_horizon.search.workspace import load_or_build_index

    root = ctx.obj["root"]
    cfg, _ = load_workspace(root)
    index = load_or_build_index(root, cfg, reindex=reindex)

    if not index.declarations:
        log.warn("No Lean declarations indexed yet. Run `lake build` to fetch sources, then `horizon search --reindex`.")
        raise typer.Exit(1)

    if type_pattern is not None:
        hits = index.search_type(type_pattern, limit=limit, library=library)
        mode = f"type ~ {type_pattern!r}"
    elif name is not None:
        hits = index.search_name(name, limit=limit, library=library)
        mode = f"name ~ {name!r}"
    elif query:
        hits = index.search_text(query, limit=limit, library=library)
        mode = f"text ~ {query!r}"
    else:
        raise typer.BadParameter("give a query, or use --name / --type")

    if as_json:
        payload = [
            {
                "name": h.declaration.name,
                "kind": h.declaration.kind,
                "signature": h.declaration.signature,
                "doc": h.declaration.doc,
                "library": h.declaration.library,
                "file": h.declaration.file,
                "line": h.declaration.line,
                "score": round(h.score, 4),
            }
            for h in hits
        ]
        print(_json.dumps(payload, indent=2))
        return

    if not hits:
        log.info(f"No matches for {mode} across {len(index.declarations)} declarations.")
        return

    from rich.console import Console
    from rich.table import Table

    table = Table(border_style="dim", padding=(0, 1), title=f"Lean search ({mode})")
    table.add_column("Name", style="bold #93c5fd", no_wrap=True)
    table.add_column("Kind", no_wrap=True)
    table.add_column("Lib", no_wrap=True)
    table.add_column("Signature")
    table.add_column("Location", style="dim", no_wrap=True)
    for h in hits:
        d = h.declaration
        sig = (d.signature[:70] + "…") if len(d.signature) > 71 else d.signature
        table.add_row(d.name or "(anonymous)", d.kind, d.library, sig, f"{d.file}:{d.line}")
    Console().print(table)
