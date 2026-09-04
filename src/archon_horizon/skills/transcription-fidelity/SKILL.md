---
name: transcription-fidelity
description: Compare a rendered source page with its TeX transcription and blueprint anchor, catching signs, indices, relation symbols, omitted clauses, and page-coordinate errors before formalization relies on them.
---

# Transcription fidelity

Use this lens for PDF-only sources or any page transcription that supplies a
formalization anchor. It checks what was copied, not whether the resulting
Lean theorem is true.

## Compare

- Open the rendered page image and the complete transcription file, using the
  manifest's PDF/page offset rather than a guessed printed page number.
- Compare relation signs, inequalities, indices, quantifiers, parentheses,
  limits, names, punctuation, and material at page breaks or in footnotes.
- Check that the `\source` id resolves to the page actually inspected and that
  the transcription has not silently omitted hypotheses, cases, or notation.
- Preserve source conventions and mark genuinely unreadable or ambiguous text
  for adjudication; do not repair it by mathematical expectation.

The `page-transcriber` role acquires or writes pages. This review role is
read-only and should pass a corrected page back to source-fidelity only after
the visual comparison is reproducible. Pair with [[references]] and
[[source-fidelity]].

## Report

Give source slug, PDF/transcription paths, page index/offset, symbols checked,
exact discrepancy or `no-issue`, impact on anchors, unchecked regions, and
confidence. Do not edit the source or blueprint while reviewing.
