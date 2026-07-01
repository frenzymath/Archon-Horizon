# Blueprints & Lean Dependency Graphs (leandag)

In formal mathematics, high-level proofs are first structured as informal LaTeX blueprints. Archon Horizon bridges the gap between informal mathematical text and formal Lean 4 declarations by extracting, inspecting, and manipulating structured dependency graphs. The blueprint subsystem lives under [`blueprint/`](../../src/archon_horizon/blueprint).

---

## Table of Contents

- [1. LaTeX Blueprint Extraction](#1-latex-blueprint-extraction)
- [2. The `leandag` Graph Engine](#2-the-leandag-graph-engine)
  - [Inspecting Dependency Cones](#inspecting-dependency-cones)
- [3. Multi-Project Graph Analysis](#3-multi-project-graph-analysis)

---

## 1. LaTeX Blueprint Extraction

Archon Horizon parses LaTeX-subset blueprints embedded within project documentation. It identifies theorems, definitions, lemmas, and their stated dependency relationships, converting them into structured JSON Directed Acyclic Graphs (DAGs). Parsing is implemented in [`blueprint/parser.py`](../../src/archon_horizon/blueprint/parser.py), with the node/edge model in [`blueprint/model.py`](../../src/archon_horizon/blueprint/model.py) and chapter grouping in [`blueprint/chapters.py`](../../src/archon_horizon/blueprint/chapters.py):

```json
{
  "nodes": [{ "id": "thm:main_result", "title": "Main Theorem", "status": "stated" }],
  "edges": [{ "source": "lem:aux_estimate", "target": "thm:main_result" }],
  "dangling": []
}
```

To parse and serialize blueprints for all projects in the workspace (see [`commands/blueprint.py`](../../src/archon_horizon/commands/blueprint.py)):

```bash
horizon blueprint
```

Outputs are written to `.archon-horizon/blueprints/<project>.json`.

---

## 2. The `leandag` Graph Engine

Horizon embeds the **leandag** analysis engine directly into the CLI, enabling instant offline topological queries and effort estimations without starting a local web server. The graph engine is [`blueprint/leandag_graph.py`](../../src/archon_horizon/blueprint/leandag_graph.py) (built on [`blueprint/dag.py`](../../src/archon_horizon/blueprint/dag.py)), exposed through [`commands/leandag.py`](../../src/archon_horizon/commands/leandag.py).

### Inspecting Dependency Cones
When an agent or researcher wants to formalize a target theorem, they need to understand its entire transitive dependency cone:

```bash
horizon leandag --node thm:main_result
```

Output summarizes:
- **Direct Dependencies**: Lemmas immediately referenced in the proof step.
- **Full Transitive Cone**: Every prerequisite node down to foundational axioms.
- **Reverse Dependencies (`rdeps`)**: Theorems that rely on this node.

---

## 3. Multi-Project Graph Analysis

In multi-library workspaces, theorems in one project may depend on definitions exported by another. `horizon leandag` can calculate set-theoretic operations across multiple project DAGs:

```bash
# View the union graph across multiple member projects
horizon leandag project_a project_b --union

# Find overlapping shared nodes (intersection) between project blueprints
horizon leandag project_a project_b --intersect
```

If signature conflicts are detected between shared node IDs across projects, the command warns explicitly and halts execution, preventing subtle formalization mismatches. Cross-project consistency checks live in [`blueprint/checks.py`](../../src/archon_horizon/blueprint/checks.py) and [`blueprint/workspace.py`](../../src/archon_horizon/blueprint/workspace.py).
