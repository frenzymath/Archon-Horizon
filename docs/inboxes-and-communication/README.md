# Inboxes & Communication Guide

Communication between humans, orchestration agents, and individual member projects in Archon Horizon is handled through a structured **dual inbox system**. This design ensures asynchronous, traceable collaboration without disruptive live prompt injections during ongoing proof searches. The inbox model lives in [`core/inbox.py`](../../src/archon_horizon/core/inbox.py), with pluggable backends under [`inboxes/`](../../src/archon_horizon/inboxes).

---

## Table of Contents

- [1. Local Filesystem Inbox](#1-local-filesystem-inbox)
  - [Inbox Kinds](#inbox-kinds)
  - [Common Local Inbox Commands](#common-local-inbox-commands)
  - [Audiences and Direct Messages](#audiences-and-direct-messages)
  - [Ownership Tiers: Shared vs. a Task's Inbox](#ownership-tiers-shared-vs-a-tasks-inbox)
  - [Per-Team Read-State](#per-team-read-state)
  - [Provenance Defaults Inside a Run](#provenance-defaults-inside-a-run)
  - [Concurrent-Safe IDs and One-Shot Items](#concurrent-safe-ids-and-one-shot-items)
- [2. Standing Protections (Soft Freeze)](#2-standing-protections-soft-freeze)
- [3. GitHub Inbox Integration](#3-github-inbox-integration)
  - [Label Gating](#label-gating)
- [4. Deterministic Sync Boundaries](#4-deterministic-sync-boundaries)

---

## 1. Local Filesystem Inbox

The local inbox stores persistent communication items directly inside `.archon-horizon/inbox/local/`. Items are categorized by semantic kind and can be scoped to specific projects, files, declarations, or blueprint nodes. The filesystem backend is [`inboxes/filesystem.py`](../../src/archon_horizon/inboxes/filesystem.py); the CLI lives in [`commands/inbox.py`](../../src/archon_horizon/commands/inbox.py).

### Inbox Kinds

| Kind | Purpose |
| :--- | :--- |
| `hint` | Guidance or proof sketches suggested by humans or agents to assist complex proof searches. |
| `issue` | Bug reports, build failures, or architectural inconsistencies requiring resolution. |
| `protection` | Standing constraints preventing modifications to specific files, declarations, or signatures. |
| `info` | General contextual notes or notifications. |
| `memory` | Long-term knowledge items retained across runs to inform future formalization strategies. |

### Common Local Inbox Commands

```bash
# Create a new inbox item (first paragraph is the title)
horizon inbox add --kind hint --body $'Use Mathlib lemma X\n\nDetailed explanation...'

# List active inbox items
horizon inbox list

# Show full details and comments for an item
horizon inbox show <id>

# Add a progress comment to an existing item
horizon inbox comment <id> --body "Investigating dependency failure..."
```

### Audiences and Direct Messages

Where a `scope` says what an item is *about*, its **audience** (`--to`) says who
it is *for*. The recognized audiences are `horizon`, `human`, `project:<name>`,
and — for a private hand-off — `task:<id>` or `run:<id>`. The general audiences
broadcast (any Horizon session on a matching project sees the item); the
`task:`/`run:` audiences are **direct messages** that reach only that one
recipient. Delivery is decided by `reaches_horizon` in
[`core/inbox.py`](../../src/archon_horizon/core/inbox.py): a session reading its
own inbox never sees a direct message meant for a different task or run, while a
see-all context (e.g. a Ground audit, where task/run are unknown) bypasses the
gating and sees everything.

```bash
# Message another project's Horizon agent
horizon inbox add --kind info --to project:mathlib-port --body $'Renamed lemma\n\nFoo.bar is now Foo.baz.'

# Direct message to one task (a private hand-off)
horizon inbox add --kind hint --to task:T-0042 --body $'Try induction\n\nInduct on the recursion depth.'
```

### Ownership Tiers: Shared vs. a Task's Inbox

Every item sits in one of two ownership tiers. By default it is **shared** —
visible to every team. Alternatively it can be **owned by a single task**, which
gives that task a private per-team inbox (e.g. a memory note only that team
keeps). Ownership is stored as `metadata.owner_task` (empty/absent means "owned
by everyone"), read via `item_owner`, and enforced in `reaches_horizon`: an owned
item reaches only its owning task.

```bash
# Create an item owned by a task (its private inbox)
horizon inbox add --kind memory --owner T-0042 --body $'Local convention\n\nUse `simp` sets, not ad-hoc rewrites.'

# ...or owned by my own task, inferred from the session
horizon inbox add --kind memory --mine --body $'Note to self\n\n...'

# Move an existing item between tiers
horizon inbox own <id> --owner T-0042   # into that task's inbox
horizon inbox own <id> --mine           # into my task's inbox
horizon inbox own <id> --shared         # back to everyone
```

A **task's inbox** is the union of the items it owns *plus* all shared items:

```bash
horizon inbox list --task T-0042   # T-0042's owned items + shared items
horizon inbox list --mine          # same, for my task (from the session)
```

The union is what `InboxFilter.owner_task` computes — it keeps an item when it is
shared *or* owned by the requested task. Ownership lives in
[`core/inbox.py`](../../src/archon_horizon/core/inbox.py) (`item_owner`,
`InboxFilter.owner_task`) and is set on the store by `set_owner` in
[`inboxes/filesystem.py`](../../src/archon_horizon/inboxes/filesystem.py).

### Per-Team Read-State

Read/unread is tracked **per reader**, not globally, so a shared item several
teams see records who has already read it. Each reader — a task, a run, or a
human — is stored in `metadata.read_by`, and "unread for me" is computed from
that list. The reader id is inferred from the session by `reader_id` (task id,
else run id, else agent role, else `human`).

```bash
horizon inbox read <id>      # mark read by me
horizon inbox unread <id>    # revert (e.g. still relevant / unactioned)
horizon inbox list --unread  # only what I have not read
```

The primitives are `item_readers` / `is_read_by` and `InboxFilter.unread_for` in
[`core/inbox.py`](../../src/archon_horizon/core/inbox.py), with `set_read` in
[`inboxes/filesystem.py`](../../src/archon_horizon/inboxes/filesystem.py).

### Provenance Defaults Inside a Run

Inside a run the orchestrator exports `ARCHON_HORIZON_*` environment variables, so
an agent rarely needs to pass the author, owner, reader, or project explicitly —
they default from the session. `provenance_task` supplies `--mine`/`--task`,
`reader_id` supplies the read-state identity, and `provenance_project` supplies
`--project` (the session's primary project); see
[`commands/shared.py`](../../src/archon_horizon/commands/shared.py). A human at the
CLI with no run environment simply passes the flags as needed.

### Concurrent-Safe IDs and One-Shot Items

`create_item` allocates the next `I-NNNN` id under an OS `flock` (`_create_lock`
in [`inboxes/filesystem.py`](../../src/archon_horizon/inboxes/filesystem.py)), so
parallel `horizon inbox add` calls get distinct ids instead of colliding on the
same slot. The guard fails *open* — on a platform without `fcntl` or after a lock
timeout it proceeds unlocked rather than refuse the write.

Items tagged `[temporary]` (via `horizon inbox add --temporary`) are one-shot
notes meant to be consumed within a single run. Any that remain open from *before*
the current run are soft-archived when the run finishes, by
`_archive_consumed_temporaries` in
[`orchestration/orchestrator.py`](../../src/archon_horizon/orchestration/orchestrator.py);
items the agent created during the run are kept for the next one.

---

## 2. Standing Protections (Soft Freeze)

When coordinating automated proof searches across large mathbases, certain foundational definitions or API signatures must remain stable. You can establish standing protections using:

```bash
horizon inbox protect --declaration Foo.bar --body "Do not modify the signature of Foo.bar during autoformalization."
```

Protections are persistent inbox items the Horizon agent pulls at session start.
They can express semantic constraints, but are not mechanically enforced. For an
exact file, declaration, blueprint node, project, or agent that must be blocked
before dispatch, use the config-backed freeze commands:

```bash
horizon freeze add file 'Core/API.lean'
horizon freeze add declaration 'Core.Api.signature'
horizon freeze add blueprint-node 'thm:stable-api'
horizon freeze list
horizon freeze remove file 'Core/API.lean'
```

These commands maintain the top-level `freeze:` section in `config.yaml`;
enforcement of declared task write sets is handled by
[`core/freeze.py`](../../src/archon_horizon/core/freeze.py).

---

## 3. GitHub Inbox Integration

For collaborative teams working across remote repositories, Archon Horizon supports shadow synchronization with GitHub issues and pull requests via the official GitHub CLI (`gh`). The GitHub backend is [`inboxes/github.py`](../../src/archon_horizon/inboxes/github.py), driven by [`horizon sync`](../../src/archon_horizon/commands/sync.py) and reconciled in [`orchestration/sync.py`](../../src/archon_horizon/orchestration/sync.py).

### Label Gating
To prevent untrusted or noisy issues from distracting autonomous agents, GitHub sync is gated by the same triage labels used for local items (defined in [`core/labels.py`](../../src/archon_horizon/core/labels.py)):
- `agent-ready`: Released to the AI agents — they may read and act on it.
- `not-ready`: Seen by a human but deliberately withheld from the agents.
- `rejected`: Dismissed; the agents never see it.

An unlabeled item is *new / untriaged*. The `import_policy` (`all` / `labeled-only` / `accepted-only`) decides which of these get pulled in — see the [Configuration guide](../configuration/README.md#6-github). Set `github.repo` in `config.yaml` to enable synchronization.

---

## 4. Deterministic Sync Boundaries

To maintain reproducible run trajectories, Archon Horizon observes inbox items and external hints **strictly at synchronization boundaries** (between collaboration rounds; see [`orchestration/orchestrator.py`](../../src/archon_horizon/orchestration/orchestrator.py)). Live prompt injection is disabled, ensuring clean state snapshots during long-running Lean compilation steps.
