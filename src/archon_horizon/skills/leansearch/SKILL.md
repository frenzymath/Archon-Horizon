---
name: leansearch
description: Find existing lemmas/definitions in mathlib, workspace projects, and configured libraries with `horizon search`; use external Mathlib search tools (LeanSearch/Loogle/Moogle/LSP) when broader Mathlib search is needed before writing proofs.
---

`horizon search` is an offline declaration search over the libraries listed in
`external_libraries` (mathlib by default) plus the workspace's own projects. It
needs no GPU, API key, or network. Reach for it before proving something
nontrivial — the premise you need often already exists.

Three local modes share one cached index:

- Natural language — what a lemma says:
  `horizon search "continuous function on a compact set attains its maximum"`
- `--name` — find a declaration by partial name:
  `horizon search --name isCompact.image`
- `--type` — find by shape of the statement; `?a`/`?b`/`_` are wildcards:
  `horizon search --type "Continuous ?f -> IsCompact ?s -> IsCompact (?f '' ?s)"`

Useful flags: `-n K`, `--lib NAME`, `--json`, `--reindex`.

Use the right search source:

- For local workspace declarations and imported non-mathlib libraries, prefer
  `horizon search`; it is optimized for the checked-out sources in this workspace.
- For broad Mathlib precedent search, also use specialized tools when available:
  LeanSearch (`lean_leansearch`), Loogle (`lean_loogle`), Moogle, or the Lean LSP
  MCP search tools. Try multiple phrasings: project name, mathematical concept,
  and conclusion/type pattern.
- Treat search hits as candidates. Read the signature/source and verify with the
  Lean checker before depending on the result.

If results look thin, run the project build so sources are fetched/current, then
`horizon search --reindex`.
