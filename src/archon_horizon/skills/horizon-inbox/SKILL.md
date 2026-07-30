---
name: horizon-inbox
description: Read and act on the Archon Horizon inbox — list/filter items, understand labels/kinds, comment, archive, create; per-team ownership and read-state; and message other projects or teams via the `horizon inbox` CLI.
---

The inbox is the durable channel between the human, agent sessions (present and
future), and other projects. Act on it through the CLI; never edit inbox files
by hand. Prefer `--json` whenever reading command output for exact ids or
fields.

## Read

- `horizon inbox list` — read the inbox. Use `--json` when you need exact ids,
  labels, scope, author, audience, timestamps, or comments. Use the list filters
  to narrow without losing the overview: `--to horizon`, `--to human`,
  `--project P`, repeated `--kind K`, repeated `--label L`,
  `--status open`, `--query TEXT`, `--limit N`, and `--comments N`.
- In an agent session, an unqualified list means the **open working queue**, not
  archived history. It is ordered by attention: required protections first,
  unread/direct conversations next, then advisory items. JSON starts with an
  `attention` summary so redirecting stderr cannot erase this signal.
- `horizon inbox show <id> --json` opens exactly one item and marks it read for
  your team. Use `--no-mark-read` for inspection without acknowledgement.
- Every inbox command evaluates the full local working set, even when `list` is
  filtered. Treat its advisory warnings as a prompt to review duplicates, stale
  memories, and consumed notices. The CLI never archives automatically because
  only the working agent can decide what is resolved; that agent must make an
  explicit cleanup pass before its final report.
- Labels are the release gate. Local inbox items default to `agent-ready`, which
  means they are visible to agents at run boundaries. Keep that default for
  machine-addressed work, and usually keep it for human-addressed notices too:
  it helps later agents see that the human has already been notified and avoids
  duplicate reports. Use `not-ready`, `rejected`, or no labels only when there is
  a concrete reason agents should not see or act on the item yet.
- Audience says who should read the item: empty/general, `horizon`, `human`,
  `project:<name>`, or a **direct message** to another team: `task:<id>` /
  `run:<id>` (older items may carry the legacy `ground` audience). A conversation
  may have several recipients; start one with `horizon inbox dm <recipient>...`.
  Scope/project says what the item is about.
- Horizon reads general items, items addressed to `horizon`, items addressed
  to its project, and DMs addressed to its own task/run. It should ignore other
  projects' and other teams' addressed items.
- A conversation's participants are its recipients **and its initiator**. The
  initiator is recorded as `metadata.started_by`; replies become unread for every
  other participant, including that initiator. Participants are not ownership:
  the initiator remains responsible for deciding when the thread is finished.
- Kinds: `conversation` (a direct/group thread requiring prompt acknowledgement),
  `hint` (normal guidance), `issue` (problem to fix/report),
  `protection` (standing constraint / soft freeze), `info` (a notice for the
  human — something you did or noticed they should know, e.g. a renamed project
  or an important change; purely informational, never affects what runs),
  `memory` (durable agent note or dead end).

## Ownership & read-state (your team's inbox)

Each running session is a **team**, and the inbox is shared across teams. Two
lightweight overlays keep it navigable:

- **Ownership** — an item is either *shared with everyone* (the default) or
  *owned by one task* (that team's private inbox — e.g. its own memory). Read your
  team's inbox with `horizon inbox list --mine` (owned-by-you **plus** shared);
  another team's private items don't appear. Own an item to your task with
  `horizon inbox add --mine …` (or `--owner <task-id>`), and `horizon inbox own
  <id> --mine|--owner <task>|--shared` to move an existing one. Owning your
  `--kind memory` notes keeps another team's memory out of your working set.
- **Read-state** — read/unread is **per team**, so a shared item several teams see
  tracks who has read it. `horizon inbox list --unread` is what *you* have not read
  yet; `horizon inbox read <id>` marks it read, and `horizon inbox unread <id>`
  puts it back (e.g. you read it but it still needs action). Combine:
  `horizon inbox list --mine --unread` is your team's fresh queue — the same signal
  the pre-command synchronizer counts for you.

Read in this order before editing:

1. `horizon inbox list --mine --status open --kind protection --json` — every
   active protection is required even if your team previously marked it read.
2. `horizon inbox list --mine --unread --kind conversation --json` — open each
   result with `inbox show`; reply when a response is needed, otherwise the read
   acknowledgement clears it until another participant replies.
3. Consult hint/issue/info/memory items as advisory context for the task.

The reader/owner id is inferred from your session (your task), so you rarely pass
it explicitly.

## Act

Authorship is automatic: every item and comment you create through the CLI is
attributed to your role (normally `horizon`) from the run environment — you do
**not** pass `--author` (a stray `--author work-reviewer` is demoted to the
`agent` metadata, not used as the author, so the author set stays conventional).
If you are a dispatched **subagent**, add `--agent <your-name>` (e.g. `--agent
work-reviewer`) to record your identity; it is stored in metadata and shown
next to the role in the UI, keeping the author itself a clean role. Items and
comments are also auto-tagged with the run/session provenance (a small chip in the
UI); you do not pass this yourself.

New inbox items use a two-part body:

- First paragraph = concise title. Keep it short enough to scan in one UI row
  (aim for under ~100 characters); do not put the full explanation here.
- Second paragraph and later = non-empty description. Put the evidence, impact,
  next action, and relevant ids/files here. The description is **mandatory** — an
  item that is only a title is rejected.

Write bodies and comments in **Markdown**: the dashboard renders it. Operational
messages are deltas, not reports. Default to one sentence or at most three short
bullets covering conclusion, new evidence, and next action; do not repeat the
thread, commit diff, or session history. Use headings only when a genuinely
multi-part decision needs them. Agent comments target at most 600 characters and
are rejected above 1200; descriptions/opening messages target 800 and are
rejected above 2400. Humans are not subject to these bounds.

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

### Conversation lifecycle

A conversation is for an expected reply, decision, or active coordination. A
one-way handoff or announcement is an `info`/`hint`, not a conversation.

1. **Search before starting:** inspect open conversations for the same topic and
   participants (`inbox list --status open --kind conversation --query "key words"
   --json`). Reply to the existing thread whenever it can carry the update. Do not
   open one thread per incremental finding.
2. **One question, one thread:** keep follow-ups, answers, corrections, and the
   final decision as comments on that thread. Start another only when it has a
   genuinely different decision boundary or participant set.
3. **Answer before branching:** when a conversation becomes unread, open it and
   answer or acknowledge it before starting another conversation with the same
   team.
4. **The initiator closes:** if `metadata.started_by` is your task/run, you own
   the lifecycle. Once the requested answer has arrived and been consumed, add a
   short conclusion comment when the outcome is not obvious, then
   `horizon inbox archive <id>`. Do this during the session, not only after a
   warning says the inbox is full.
5. **Do not hide an answer from its initiator:** recipients normally reply and
   leave closure to the initiator. For a human-started thread, answer it but leave
   it open for the human to consume/archive unless the human explicitly delegates
   closure. Use `complete` instead of `archive` only when the concluded thread is
   intentionally meant to remain visible in the resolved working record.
6. **End-of-session sweep:** before the final report, inspect open conversations
   in which your task is `started_by`. Archive every resolved one and state a real
   blocker on any that must remain open. An accumulation of open conversations is
   unfinished coordination, not harmless history.

- `horizon inbox comment <id> --body "..."` — progress note on an item (record
  what changed; not to ask the human for clarification). Do not write your name
  into the body; authorship is automatic. Comment only at a **key advance** such
  as a milestone, ruled-out route, blocker, correction, or conclusion. Update the
  existing thread with the delta instead of re-establishing its full context.
- `horizon inbox dm task:<id> [task:<id> ...] --body "Topic\n\nOpening message"`
  — start a one-to-one or group conversation. Use `horizon ps` to discover live
  teams. Use it only after the lifecycle search above. The sender remains a
  participant and owns closure; every reply makes the thread unread for the other
  participants. Humans read and reply from the live dashboard.
- **Write comments as mathematics, for a human.** State the key step that
  succeeded in human-readable mathematical language (LaTeX for formulas), naming
  the result and the idea — e.g. "Proved $H^i(X,\mathcal{F})=0$ for $i>\dim X$ by
  reducing to the affine case via Čech cohomology." Mention Lean declaration
  names and file paths only as supporting detail; the substance is the maths, not
  a code changelog. This applies to comments on **roadmap items and tasks** too:
  when a key step lands, record it there in the same mathematical, legible style.
- `horizon inbox archive <id>` — soft-delete: keep the item for the record but
  hide it from the dashboard by default. Use for stale, superseded, consumed, or
  no-longer-key context. Keeping the inbox tidy this way is your job (or the
  janitor subagent's) — prune memory/info so they remain small.
  For a conversation you initiated, this is the normal "discussion finished"
  operation after its answer/conclusion has been consumed.
- `horizon inbox complete <id>` — mark an item resolved only when it records work
  you genuinely concluded and want to keep visible outside the archive. Before
  completing, add a short **conclusion comment**: first the mathematical
  conclusion (what was proved/established, in words + LaTeX), then concise
  metadata (runs, approximate LOC, files/declarations involved). A reader should
  grasp the result and how it was concluded without opening the logs.
- `horizon inbox add --body "Short title\n\nDescription with details and next action." [--to R] [--project P] [--persistent|--temporary] [--agent <name> if a subagent]`
  — open a new item.
  - `--to` is the recipient: `horizon`, `human`, `project:<name>`, or a direct
    message to another team `task:<id>` / `run:<id>`.
    Use `--to project:<name>` to message another project (e.g. "I rewrote your
    `foo` declaration more cleanly, you may want it"); use `--to task:<id>` to
    message a specific running team.
  - `--owner <task-id>` / `--mine` keeps the item in one team's inbox instead of
    the shared default.
  - `--project` says what the item is ABOUT (its subject).
  - `--persistent` / `--temporary` just prepend a `[persistent]` / `[temporary]`
    tag to the body.
  - `--pending` labels the item `not-ready` instead of the default `agent-ready`,
    so it stays invisible to agents until a human releases it.
- `horizon inbox add --kind memory --to horizon --body "Short memory title\n\nDurable fact, convention, or dead end."` — record a durable
  note / dead end. Memory lives in the inbox, so it is rendered in the "Memory"
  section and a human can prune it like any other item.
- `horizon inbox protect --body "..." [--project P] [--file F] [--declaration D] [--blueprint-node N]`
  — add a semantic standing constraint the Horizon agent must respect (a soft freeze),
  e.g. "do not change the signature of `Foo.bar`". These render as a
  "Protected" section; honour them, including semantic ones. For a target that
  must be mechanically blocked before dispatch, the user uses the config-backed
  `horizon freeze add <file|declaration|blueprint-node> <target>` command instead.

Reading convention: an item tagged `[persistent]` is a standing rule — respect
it every round and keep it open while relevant. `[temporary]` (or untagged) is
one-shot — archive it once consumed. Items in the "Protected" section are never
modified.
