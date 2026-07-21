# Inboxes & Communication Guide

Communication between humans, orchestration agents, and individual member projects in Archon Horizon is handled through a structured **dual inbox system**. This design ensures asynchronous, traceable collaboration without disruptive live prompt injections during ongoing proof searches. The inbox model lives in [`core/inbox.py`](../../src/archon_horizon/core/inbox.py), with pluggable backends under [`inboxes/`](../../src/archon_horizon/inboxes).

---

## Table of Contents

- [1. Local Filesystem Inbox](#1-local-filesystem-inbox)
  - [Inbox Kinds](#inbox-kinds)
  - [Common Local Inbox Commands](#common-local-inbox-commands)
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
