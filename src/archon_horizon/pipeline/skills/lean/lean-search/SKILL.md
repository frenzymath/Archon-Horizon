---
name: lean-search
description: Find and verify existing Lean or mathlib declarations before implementing a proof, replacing duplicate infrastructure, or choosing a reusable definition.
metadata:
  category: lean
license: MIT
---

# Lean Search

Start with the goal's type and the project's pinned dependencies. Search is a
way to shorten the proof, not a separate deliverable or an exhaustive survey.

1. Identify the mathematical operation, objects, hypotheses, and conclusion.
   Search nearby project declarations first, then the installed dependency.
   Look for a stronger theorem, a characterization, or an existing structure
   whose API already supplies the needed result.
2. Use the tool matching the uncertainty. Search names or text with local
   source search; use a type-pattern search when the signature is known; use
   natural-language search when only the mathematical concept is known.
   Available LSP tools are useful, but this skill requires no particular MCP
   server. `rg` plus Lean's own elaborator is a sufficient fallback.
3. Read candidate statements and their namespace, assumptions, and imports.
   Confirm a candidate at the checked-out revision with hover information,
   `#check`, or a small application in the target file. Check implicit parameters
   when an apparently matching lemma fails to apply.
4. Try the candidate in the actual goal. Different coercions, universes,
   topology/measurable-space instances, or side conditions can make a search
   hit inapplicable. A web result or different branch's index is not local
   availability evidence.
5. When reasonable alternate names, neighboring files, and more general
   formulations do not help, prove the missing fact. Record a useful missing
   API or failed route once; do not repeatedly rerun the same search.

For local source search, restrict scope before increasing output:

```bash
rg -n 'compact.*image|image.*compact' .lake/packages/mathlib/Mathlib/Topology
rg -n 'theorem|lemma|def|structure' MyProject/RelevantModule.lean
```

Resolve dependency paths from this repository's Lake setup; the usual mathlib
path above is an example. Use the Horizon search API for published repositories
at a stated commit and inspect the returned indexed revision. Keep draft or
unpublished source searches local when an external search would disclose it.

In a disposable proof experiment, `exact?`, `apply?`, `rw?`, or `simp?` can suggest
existing facts when available in the pinned toolchain. Keep the successful
explicit suggestion when clearer; do not leave every discovery command in a
published proof. Follow `horizon-formalization` for managed build admission.

Choose imports that expose the actual declaration and required instances.
Avoid importing all of mathlib merely to make one search result available;
follow the destination repository's import and module-visibility conventions.

Read [search routes](references/search-routes.md) when name searches fail or
the missing piece may be a more general theorem, equivalent formulation, or
canonical abstraction.

Adapted from [lean4-skills](../../_sources/lean4-skills/PROVENANCE.md).
