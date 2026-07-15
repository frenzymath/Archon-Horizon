---
name: leansearch
description: Find existing lemmas/definitions before proving — the local `horizon search` index plus the Lean LSP MCP search tools (LeanSearch/Loogle/state-search/hammer-premise). Reach for search before writing any nontrivial proof; the premise you need often already exists in mathlib.
---

**Search before you prove.** Most facts you need already exist in mathlib or the
workspace. Searching is far cheaper than reproving. Use the local index first
(unlimited, instant), then the LSP search tools for broader semantic/type search.

## Local first: `horizon search`

An offline declaration search over `external_libraries` (mathlib by default) plus
the workspace's own projects. No GPU, API key, or network. Three modes share one
cached index:

- Natural language — what a lemma says:
  `horizon search "continuous function on a compact set attains its maximum"`
- `--name` — find a declaration by partial name:
  `horizon search --name isCompact.image`
- `--type` — find by shape of the statement; `?a`/`?b`/`_` are wildcards:
  `horizon search --type "Continuous ?f -> IsCompact ?s -> IsCompact (?f '' ?s)"`

Useful flags: `-n K`, `--lib NAME`, `--json`, `--reindex`. If results look thin,
run the project build so sources are current, then `horizon search --reindex`.

## Lean LSP MCP search tools

The `lean-lsp` MCP server (configured in `.mcp.json`) exposes a full search
arsenal. Rate limits are **per-tool** (separate pools), not a shared budget.

| Tool | Use it for | Limit |
|------|-----------|-------|
| `lean_local_search` | exact/partial declaration name; workspace + mathlib | unlimited |
| `lean_leansearch` | **semantic search by mathematical meaning** (PKU LeanSearch) | 10 / 30s |
| `lean_loogle` | search by **type shape** when you know the signature, not the name | remote; unlimited in `--loogle-local` |
| `lean_state_search` | lemmas that apply to a specific **proof state** (goal at a line) | 3 / 30s |
| `lean_hammer_premise` | **premise names** to feed `simp only [...]` / `aesop` / `grind` | 3 / 30s |
| `lean_leanfinder` | semantic fallback — only when `lean_leansearch` fails | 10 / 30s |

### Priority / escalation order

1. `lean_local_search("name")` — always first; unlimited, instant, deterministic.
2. `lean_leansearch("describe the math.")` — primary semantic search (see below).
3. `lean_loogle("?a -> ?b")` — simple type patterns only.
4. `lean_state_search(file, line, col)` — goal-conditioned lookup when stuck.
5. `lean_hammer_premise(file, line, col)` — premises for `simp`/`aesop`/`grind`.
6. `lean_leanfinder("description")` — last-resort semantic fallback.

### `lean_leansearch` — the PKU semantic tool (use it well)

LeanSearch (leansearch.net, from the PKU team) augments each mathlib theorem into
an informal description and matches your query against those embeddings —
returning the **nearest neighbours**, not just exact hits. That is its strength:
a theorem 90% of what you need is often enough; prove the small gap yourself.

- **Describe the mathematical content, not the name.** Query must end with `.`/`?`.
  - Good: `lean_leansearch("The image of a compact set under a continuous map is compact.")`
  - Good (mixed): `lean_leansearch("natural numbers. from: n < m, to: n + 1 < m + 1")`
  - Weak: `lean_leansearch("add_comm")` → use `lean_local_search` instead.
  - Weak: `lean_leansearch("∀ (a b : ℕ), a + b = b + a")` → use `lean_loogle` instead.
- **Trust a negative result.** If a precise, mathematically accurate description
  returns nothing relevant, the theorem likely isn't in mathlib — stop rephrasing
  and prove it (or find another route) rather than searching endlessly.

### `lean_loogle` — type-shape search

Searches by *type structure*, not names. Syntax: `?a`/`?b` type vars, `_`
wildcards, `->`/`→` arrows, `|- pattern` by conclusion.
- Good: `lean_loogle("(?a -> ?b) -> List ?a -> List ?b")` → `List.map`
- Anti-pattern: nested `Submodule`/`LinearMap`/`Finsupp`, 3+ typeclass
  constraints, or guessy implicit structure — use `lean_leansearch` for those.

## Discipline

- Treat every hit as a **candidate**: read the signature/source and confirm with
  the Lean checker (see the `lean-check` skill) before depending on it.
- Try multiple phrasings — project name, mathematical concept, conclusion/type.
- Local (`horizon search` / `lean_local_search`) is optimized for the checked-out
  sources here; the LSP tools reach broader mathlib precedent. Use both.
