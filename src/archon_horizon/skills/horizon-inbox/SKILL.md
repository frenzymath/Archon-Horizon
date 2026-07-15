---
name: horizon-inbox
description: Read and act on the Archon Horizon inbox — list/filter agent-ready items, understand labels/kinds, comment, archive, create, and message other projects via the `horizon inbox` CLI.
---

The inbox is the durable channel between the human, the Ground agent, the
Horizon agent, and other projects. Act on it through the CLI; never edit inbox
files by hand. Prefer `--json` whenever reading command output for exact ids or
fields.

## Read

- `horizon inbox list` — read the inbox. Use `--json` when you need exact ids,
  labels, scope, author, audience, timestamps, or comments. Use the list filters
  to narrow without losing the overview: `--to horizon`, `--to ground`,
  `--to human`, `--project P`, repeated `--kind K`, repeated `--label L`,
  `--status open`, `--query TEXT`, `--limit N`, and `--comments N`.
- Labels are the release gate. Local inbox items default to `agent-ready`, which
  means they are visible to agents at run boundaries. Keep that default for
  machine-addressed work, and usually keep it for human-addressed notices too:
  it helps later agents see that the human has already been notified and avoids
  duplicate reports. Use `not-ready`, `rejected`, or no labels only when there is
  a concrete reason agents should not see or act on the item yet.
- Audience says who should read the item: empty/general, `horizon`, `ground`,
  `human`, or `project:<name>`. Scope/project says what the item is about.
- Horizon reads general items, items addressed to `horizon`, and items addressed
  to its project. It should ignore other projects' addressed items.
- Kinds: `hint` (normal guidance), `issue` (problem to fix/report),
  `protection` (standing constraint / soft freeze), `info` (a notice for the
  human — something you did or noticed they should know, e.g. a renamed project
  or an important change; purely informational, never affects what runs),
  `memory` (durable agent note or dead end).

## Act

Authorship is automatic: every item and comment you create through the CLI is
attributed to your role (`ground` or `horizon`) from the run environment — you do
**not** pass `--author` (a stray `--author blueprint-reviewer` is demoted to the
`agent` metadata, not used as the author, so the author set stays conventional).
If you are a dispatched **subagent**, add `--agent <your-name>` (e.g. `--agent
blueprint-reviewer`) to record your identity; it is stored in metadata and shown
next to the role in the UI, keeping the author itself a clean role. Items and
comments are also auto-tagged with the run/session provenance (a small chip in the
UI); you do not pass this yourself.

New inbox items use a two-part body:

- First paragraph = concise title. Keep it short enough to scan in one UI row
  (aim for under ~100 characters); do not put the full explanation here.
- Second paragraph and later = non-empty description. Put the evidence, impact,
  next action, and relevant ids/files here. The description is **mandatory** — an
  item that is only a title is rejected.

Write bodies and comments in **Markdown**: the dashboard renders it. Use short
paragraphs and `-` bullet lists instead of one wall-of-text paragraph, `**bold**`
for the key claim, and backticks for ids, lemma names, files, and code
(`` `lem:foo` ``, `` `Foo.lean:42` ``). A long unstructured blob is hard to scan.

Do not create an item whose body is only a title. If you need a `[persistent]` or
`[temporary]` tag, put it at the start of the title paragraph and keep the rest
of that paragraph concise.

Before opening a new item, search the open inbox for the same topic using
`horizon inbox list --status open --query "key words" --json` and, when relevant,
`--project P` / `--to R`. If a live item already covers the same issue, do not
open a duplicate: add a comment with the new evidence, or edit the body if the
main description is stale. If you discover an open item is obsolete, already
solved, consumed, or superseded, add a short comment when the reason is not
obvious and then run `horizon inbox archive <id>`.

- `horizon inbox comment <id> --body "..."` — progress note on an item (record
  what you did; not to ask the human for clarification). Do not write your name
  into the comment body; the author is set automatically. Drop a comment at each **key advance**
  while you work an item (a milestone reached, a route ruled out, a blocker hit),
  not only at the end — these comments are the item's visible progress trail.
- **Write comments as mathematics, for a human.** State the key step that
  succeeded in human-readable mathematical language (LaTeX for formulas), naming
  the result and the idea — e.g. "Proved $H^i(X,\mathcal{F})=0$ for $i>\dim X$ by
  reducing to the affine case via Čech cohomology." Mention Lean declaration
  names and file paths only as supporting detail; the substance is the maths, not
  a code changelog. This applies to comments on **roadmap items and tasks** too:
  when a key step lands, record it there in the same mathematical, legible style.
- `horizon inbox archive <id>` — soft-delete: keep the item for the record but
  hide it from the dashboard by default. Use for stale, superseded, consumed, or
  no-longer-key context. Ground is responsible for keeping the inbox tidy this
  way, including pruning memory/info so they remain small.
- `horizon inbox complete <id>` — mark an item resolved only when it records work
  you genuinely concluded and want to keep visible outside the archive. Before
  completing, add a short **conclusion comment**: first the mathematical
  conclusion (what was proved/established, in words + LaTeX), then concise
  metadata (runs, approximate LOC, files/declarations involved). A reader should
  grasp the result and how it was concluded without opening the logs.
- `horizon inbox add --body "Short title\n\nDescription with details and next action." [--to R] [--project P] [--persistent|--temporary] [--agent <name> if a subagent]`
  — open a new item.
  - `--to` is the recipient: `horizon`, `ground`, `human`, or `project:<name>`.
    Use `--to project:<name>` to message another project (e.g. "I rewrote your
    `foo` declaration more cleanly, you may want it").
  - `--project` says what the item is ABOUT (its subject).
  - `--persistent` / `--temporary` just prepend a `[persistent]` / `[temporary]`
    tag to the body.
- `horizon inbox add --kind memory --to horizon --body "Short memory title\n\nDurable fact, convention, or dead end."` — record a durable
  note / dead end. Memory lives in the inbox, so it is rendered in the "Memory"
  section and a human can prune it like any other item.
- `horizon inbox protect --body "..." [--project P] [--file F] [--declaration D]`
  — add a standing constraint the Horizon agent must respect (the soft freeze),
  e.g. "do not change the signature of `Foo.bar`". These render as a
  "Protected" section; honour them, including semantic ones.

Reading convention: an item tagged `[persistent]` is a standing rule — respect
it every round and keep it open while relevant. `[temporary]` (or untagged) is
one-shot — archive it once consumed. Items in the "Protected" section are never
modified.
