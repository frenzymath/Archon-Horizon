---
name: mathlib-orientation
description: A compact map of Mathlib and workspace resources for finding canonical abstractions, documentation, examples, and proof tools before introducing a new Lean API.
recommendation: >-
  Consider this skill before reimplementing a definition or lemma. Search the
  local index and exact source context first, then record a precise no-result
  before building new infrastructure.
---

# Mathlib orientation

Use this as a resource map, not as a second proof workflow. Search is evidence
that an abstraction is present or absent; a search hit still needs a signature
and import check.

## Where to look

- Start with `horizon search` and `lean_local_search` for names and declarations
  in Mathlib and sibling workspace projects.
- Use `lean_leansearch` for a natural-language statement and `lean_loogle` for
  a type shape. Use goal-conditioned search only after the local search is
  concrete.
- Inspect the checked-out source under `.lake/packages/mathlib/Mathlib/` and
  its module docstrings when a candidate's namespace, assumptions, or imports
  are unclear. Read nearby examples and lemmas, not only the declaration name.
- Search the workspace's other projects for analogous bundled structures or
  bridges before creating a project-local duplicate.
- In a small scratch declaration, use `#check`, `#print`, `#find`, `exact?`,
  `apply?`, `rw?`, `simp?`, or `library_search` to test a candidate in the
  actual import context.

## Community documentation map

For naming, style, and docstrings, use [[mathlib-conventions]] and the upstream
guides it lists (naming, style, documentation, PR review on
leanprover-community.github.io). Those pages are authoritative when a local
summary disagrees.

## Resource discipline

Keep the import as narrow as the target allows and prefer the canonical
namespace, bundled morphism, typeclass, and theorem names. A documentation page,
index hit, or copied snippet is not evidence that the declaration elaborates in
the project's toolchain. Verify the exact version and imports with
[[lean-check]]. If a search is negative, record the query and scope before
rephrasing indefinitely or reimplementing a nearby result.

For source-backed work, consult [[source-fidelity]] and [[references]] as well;
Mathlib precedent informs an implementation but does not replace the cited
source contract. For API duplication or bridge questions, pair this map with
[[api-composition]]. When the local checkout or module docs are insufficient,
[[source-discovery]] describes how to use upstream Mathlib GitHub PRs/issues and
maintainer discussions as context while keeping the checked-out declaration as
the implementation evidence.
