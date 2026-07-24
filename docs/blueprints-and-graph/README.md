# Blueprints and semantic graphs

Horizon turns each project's leanblueprint document and Lean sources into one
plain-files semantic graph. The graph implementation is vendored under
[`archon_horizon/hgraph/`](../../src/archon_horizon/hgraph), so installing
Horizon does not fetch a separate graph library.

## Storage and synchronization

Each project owns an `hgraph/` directory:

```text
hgraph/
├── config.yaml
├── nodes/       # statements, Lean declarations, comments, and reviews
└── edges/       # uses, formalizes, and related_to relationships
```

The files are normal Markdown with YAML metadata and are versioned with the
project. Synchronize derived nodes and edges after changing the blueprint or
Lean declarations:

```bash
horizon graph --project MyProject sync
```

`sync` resolves `\label`, `\uses`, and `\lean` against real sources, preserves
authored comments/reviews, and reports unresolved references. Workspace publish
boundaries also synchronize the graph and cache the dashboard shape under
`.archon-horizon/blueprints/<project>.json`.

When it scans Lean sources, `sync` keys one node per declaration by its
fully-qualified name. The parser
([`hgraph/sync.py`](../../src/archon_horizon/hgraph/sync.py)) recognises
`theorem`, `lemma`, `def`, `abbrev`, and `instance` declarations, and also
`structure`, `inductive`, and `class` — so a `structure` (or `class`) becomes a
first-class node instead of going unmatched and leaving its `\lean{}` reference
stale. Declaration names may contain Unicode letters and subscripts (`foo₁`),
not just ASCII, and an anonymous `instance : C` (no name) is skipped. A `_root_.`
prefix escapes the enclosing namespace: `theorem _root_.Foo.bar` resolves to the
absolute name `Foo.bar`, so a `\lean{Foo.bar}` reference matches it instead of
resolving to `Namespace._root_.Foo.bar`.

## Querying and editing

The vendored implementation is available through the Horizon CLI:

```bash
horizon graph -p MyProject stats
horizon graph -p MyProject frontier --type tex
horizon graph -p MyProject get label:thm:main-result
horizon graph -p MyProject ancestors label:thm:main-result --names
horizon graph -p MyProject add comment label:thm:main-result \
  --content "The induction stalls at the successor case."
```

Blueprint `.tex` files contain timeless mathematics only. Lean implementation
details, failed proof routes, and formalization caveats belong in these node
comments, where synchronization preserves them. Agent comments automatically
record their Horizon role and run/session/task provenance.

Run `horizon graph --help` for the command list and, for example,
`horizon graph frontier --help` for one operation's flags. When a workspace has
only one project, `--project` is optional.

## Dashboard representation

The dependency page uses the chapter graph ported from hgraph rather than a
force-directed layout:

- the initial view collapses every chapter into one purple aggregate node;
- selecting a chapter expands its statements and keeps other chapters collapsed;
- Graphviz lays out dependencies deterministically in a worker;
- detail-level, status/search highlighting, pan, zoom, fit, and the existing
  statement/Lean sidebar remain available.

Definitions and conventions use rounded boxes; results use ellipses. Node
borders encode statement readiness and fills encode proof/closure state.

The adapter that publishes graph data to the dashboard is
[`blueprint/hgraph_graph.py`](../../src/archon_horizon/blueprint/hgraph_graph.py).
