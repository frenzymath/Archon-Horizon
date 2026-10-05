---
name: source-research
description: Find and interpret mathematical sources, maintain Horizon's reference catalog and BibTeX, and preserve exact evidence for graph statements and blueprints.
metadata:
  category: operations
---

# Research the actual source obligation

Start from the assigned question, existing node citations and reference catalog.
Check local source and relevant Lean libraries before proposing a second
development. An empty search-index result does not imply an empty repository;
walk its relevant modules and use direct search when necessary.

For milestone preprocessing, do this before or alongside route selection. Register
the primary sources with complete BibTeX and map proposed milestones to exact
locators before contract review. Organize shared definitions, source notes and
downloads in intentional locations; keep the durable roadmap concise. Every
accepted milestone citation must resolve to an active central reference.

Prefer a primary paper, book, original TeX, or versioned repository. A search
snippet or secondary summary can locate evidence but cannot establish theorem
hypotheses. Record the edition/version, stable identifier, precise theorem or
section locator, and definitions/standing assumptions used. Distinguish the
source's claim from your inference and its Lean encoding.

For PDFs, inspect the rendered mathematical passage when signs, quantifiers,
indices or formulas matter. OCR/text extraction helps navigation; it is not
verified transcription. Record printed page labels separately from physical PDF
page indices. Do not invent a missing clause or page number. Original TeX must
match the cited version; macros and neighboring definitions can change meaning.

Use the central reference catalog for bibliographic identity and citation keys.
It exports BibTeX, so each node need not contain another hand-maintained copy.
Use the bounded local BibTeX cache for quick access; it does not download the
paper. Reusable notes/transcriptions belong in deliberately published project
artifacts. Large disposable downloads belong under `$TMPDIR`, with the source
locator retained in durable evidence. Read [references and blueprints](references/catalog.md)
for concrete API and file workflows.

When translating a passage, list its objects, domains, quantifiers, regularity,
conventions and conclusion. Identify any missing bridge to the Lean types. A
citation proves provenance, not equivalence; load the statement-alignment skill
for a significant source-facing declaration. Preserve genuine ambiguities as
explicit obligations with an owner instead of silently choosing an easier claim.
