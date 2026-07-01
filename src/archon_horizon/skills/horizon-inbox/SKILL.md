---
name: horizon-inbox
description: Read and act on the Archon Horizon inbox — list/filter agent-ready items, understand labels/kinds, comment, close, create, and message other projects via the `horizon inbox` CLI.
---

The inbox is the durable channel between the human, the Ground agent, the
Horizon agent, and other projects. Act on it through the CLI; never edit inbox
files by hand. Prefer `--json` whenever reading command output for exact ids or
fields.

## Read

- `horizon inbox list` — read the inbox. Use `--json` when you need exact ids,
  labels, scope, author, audience, timestamps, or comments.
- Labels are the release gate. Only items labelled `agent-ready` are released to
  agents. You should filter for `agent-ready` items when reading the inbox, so that 
  you are not distracted by items that are not allowed for you to act on. 
  The human can label items `not-ready` or `rejected` or not label at all, 
  which means agents like you can't read/act on them. 
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

Agents must identify their authored local inbox changes: Ground uses `--author ground`; Horizon uses `--author horizon`. The author is mandatory on every item and comment — never omit it. Items and comments you create through the CLI during a run are also auto-tagged with the run/session provenance (shown as a small chip in the UI); you do not pass this yourself.

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

- `horizon inbox comment <id> --author ground|horizon --body "..."` — progress note on an item (record
  what you did; not to ask the human for clarification). Do not write the author
  into the comment body; use `--author`. Drop a comment at each **key advance**
  while you work an item (a milestone reached, a route ruled out, a blocker hit),
  not only at the end — these comments are the item's visible progress trail.
- `horizon inbox complete <id>` — close an item once you have acted on it. Before
  closing, add a short **closing comment** that records the conclusion and concise
  metadata: how many runs it took, approximate LOC changed, the files/declarations
  involved, and the outcome. Keep it to a few bullets — a reader should see when
  and how the item was concluded without opening the logs.
- `horizon inbox add --author ground|horizon --body "Short title\n\nDescription with details and next action." [--to R] [--project P] [--persistent|--temporary]`
  — open a new item.
  - `--to` is the recipient: `horizon`, `ground`, `human`, or `project:<name>`.
    Use `--to project:<name>` to message another project (e.g. "I rewrote your
    `foo` declaration more cleanly, you may want it").
  - `--project` says what the item is ABOUT (its subject).
  - `--persistent` / `--temporary` just prepend a `[persistent]` / `[temporary]`
    tag to the body.
- `horizon inbox add --kind memory --to horizon --author ground|horizon --body "Short memory title\n\nDurable fact, convention, or dead end."` — record a durable
  note / dead end. Memory lives in the inbox, so it is rendered in the "Memory"
  section and a human can prune it like any other item.
- `horizon inbox protect --body "..." [--project P] [--file F] [--declaration D]`
  — add a standing constraint the Horizon agent must respect (the soft freeze),
  e.g. "do not change the signature of `Foo.bar`". These render as a
  "Protected" section; honour them, including semantic ones.

Reading convention: an item tagged `[persistent]` is a standing rule — respect
it every round and never close it. `[temporary]` (or untagged) is one-shot —
`complete` it once consumed. Items in the "Protected" section are never modified.
