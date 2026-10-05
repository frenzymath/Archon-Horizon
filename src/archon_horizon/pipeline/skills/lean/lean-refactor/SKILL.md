---
name: lean-refactor
description: Refactor Lean proofs, definitions, modules, and consumers around a preserved mathematical contract, including deliberate removal of obsolete infrastructure.
metadata:
  category: lean
license: MIT
---

# Refactor From The Contract

Identify the public statement, actual consumers, import cone, and source/graph
references before editing. Separate proof-body cleanup from an API change or
changed mathematics. The last requires an explicit revised contract; a cleaner
compiling proxy is not a replacement for the original target.

Search existing abstractions and proofs. If a canonical library theorem or
definition removes custom infrastructure, verify its exact hypotheses and
bridge before changing callers. Read
[extraction and migration](references/extraction-and-migration.md) for choosing
helper boundaries and migrating a representation.

Preserve a useful checkpoint through the configured workspace/publication
workflow before a substantial rewrite. Use a disposable worktree for risky
experiments. Respect other agents' owned files; do not create backup copies or
parallel obsolete modules in the published library as a substitute for history.

Prove one real consumer against the proposed API, then migrate the needed
definitions, core lemmas, bridges, public theorem, and downstream consumers.
The review order may begin at the headline theorem while the implementation
order follows dependencies. Prefer coherent compilable increments when useful;
do not preserve a fundamentally poor module solely to minimize a diff.

Remove superseded definitions and files after checking imports, root exports,
searchable names, graph paths, blueprint links, tests, and documentation. Keep
an alias only for a known consumer or deliberate compatibility commitment, not
as permanent debris. Update the roadmap/source mapping with the code.

Check affected consumers using [lean-check](../lean-check/SKILL.md). Explain
the improvement with actual reuse, a clearer proof boundary, fewer required
transports, or measured cost. Shorter text alone does not prove better design.
Record remaining work in the ledger with an owner; a partial migration is not
a completed refactor.

Adapted from Horizon's contract-first refactoring and upstream
`proof-refactoring.md`; [provenance](../../_sources/lean4-skills/PROVENANCE.md).
