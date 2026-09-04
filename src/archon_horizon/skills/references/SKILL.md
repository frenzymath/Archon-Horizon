---
name: references
description: The shared workspace-root references/ library and its manifest.yaml — how to find, read, and cite original sources with \source{...}. Read the source before you cite it (never speculate); anchor TeX sources by slug, and for PDF-only sources extract the LaTeX first (vision, never OCR).
---

`references/` is a **shared library at the workspace root** — one library for
every project — holding the original source material a blueprint is formalised
from (papers, books, notes). It is indexed by `references/manifest.yaml`. Per-
project files live under each project's directory; `references/` does not — it is
always at the workspace root, and `references/**` is writable from any agent.

A blueprint node derived from a source carries a `\source{...}` anchor. Those
anchors are the audit trail from a formal statement back to where it came from,
so they must be **true**, not plausible.

## The one rule: read before you cite

**Never add a `\source{...}` anchor to a source you have not actually read.** Do
not infer a citation from a title, a topic, a theorem number, or your own prior
knowledge of the result. An anchor asserts "this exact statement/proof appears
here" — if you haven't opened the referenced material and confirmed it, you don't
know that, and a wrong anchor is worse than none (it sends the next reader/agent
to the wrong place and launders a guess as provenance).

Concretely, before writing `\source{...}`:
1. Confirm the source exists in `references/manifest.yaml`. If it isn't there,
   get it in first (see below) — don't cite a slug that doesn't resolve.
2. Open the actual content — the TeX, or the transcribed page — and confirm the
   statement/proof you're anchoring is really there.
3. Only then write the anchor.

## Anchor forms — pick by what the source actually is

The manifest's `read` field for each entry tells you which case you're in.

- **The source has LaTeX** (arXiv e-print, an open `.tex`, etc.): read the TeX
  directly and cite the source as a whole — `\source{slug}`. Do **not** invent a
  `page-NNNN`; a TeX source has no rendered pages, so a page number would be
  fabricated. If the manifest documents stable sub-ids for the source (a section
  or label id), you may use `\source{slug:<id>}`, but only an id that exists.
- **The source is PDF-only** (no usable TeX): cite the transcribed page,
  `\source{slug:page-NNNN}`, where `page-NNNN` is a real file under
  `references/<slug>/tex/page-NNNN.tex` that you have read. If that page hasn't
  been transcribed yet, transcribe it first (below) — don't cite a page that
  doesn't exist on disk.

Multiple anchors may be comma-separated: `\source{slug:page-0042, other:page-0007}`.

## Getting a source into the library

Don't fetch and stash sources ad hoc. Spawn the **`reference-retriever`** subagent
(by name, through your engine's native subagent mechanism) with whatever you have
— topic, arXiv ID, DOI, title, URL, target page range. It downloads the original
(preferring TeX, keeping the PDF), verifies the file is real (not a paywall/login
page named `.pdf`), organises it under `references/<slug>/`, and registers it in
`references/manifest.yaml`. Wait for it, then cite from what it wrote.

## Extracting the LaTeX from a PDF-only source

When there is no reliable TeX and you need the actual math (to read it, or to cite
a page), **extract it with the proper tool — don't eyeball the PDF and paraphrase.**
The extraction path is the **`page-transcriber`** subagent, which renders each page
to an image and **vision-transcribes** it into one self-contained
`references/<slug>/tex/page-NNNN.tex` per page.

- **Vision, never OCR.** OCR mangles math (subscripts, operators, alignment);
  reading the rendered page image with a vision model preserves it. This is a hard
  rule of the transcription flow.
- **A small vision model is usually enough.** Faithful page transcription doesn't
  need a frontier model — a cheap vision-capable model typically suffices. The
  Horizon agent chooses the model and effort at dispatch time; do not encode that
  spend policy in `config.yaml` or the descriptor.
- **Keep each call ≤5 pages.** Longer ranges lose accuracy; split a big range
  across several `page-transcriber` calls.

## Reading transcriptions correctly

- A transcribed page is `references/<slug>/tex/page-NNNN.tex` where `NNNN` is the
  **PDF page index**, which often differs from the printed page number. The
  manifest entry's `read` field records the `page_offset` and any fallback that
  actually worked — respect it when mapping "Theorem 3.4 on p. 12" to a file.
- Read the whole page file before citing it; a page can hold several results.

## The manifest at a glance

One entry per source (the `reference-retriever` owns the full schema):

```yaml
references:
  - slug: <slug>
    title: <title>
    source: <arXiv ID / DOI / URL>
    status: ok            # or: not_found
    files:
      - { path: <slug>/<slug>.pdf, format: pdf }
      - { path: <slug>/source.tex, format: tex }        # if TeX obtained
      - { path: <slug>/tex/page-0042.tex, format: tex } # if transcribed
    read: "Read TeX when present; else page anchors slug:page-NNNN. page_offset: ..."
```

If a source can't be found and verified, it's recorded `status: not_found` — treat
that as "no citation available," and write no math attributed to it.

Related skills: `blueprint-conventions` for where `\source{...}` sits among the
other blueprint annotations, and `hgraph` for how a node's `sources` feed the DAG.
For material outside the local library, use `source-discovery` to choose among
GitHub history, Mathlib source and PRs, Tau Ceti review artifacts, and Zulip
context. External discussion is context until the underlying source or checked
declaration confirms the claim.
