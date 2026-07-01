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

The local inbox stores persistent communication items directly inside `.archon-horizon/inboxes/`. Items are categorized by semantic kind and can be scoped to specific projects, files, or declarations. The filesystem backend is [`inboxes/filesystem.py`](../../src/archon_horizon/inboxes/filesystem.py) (with a sharded variant in [`inboxes/sharded.py`](../../src/archon_horizon/inboxes/sharded.py)); the CLI lives in [`commands/inbox.py`](../../src/archon_horizon/commands/inbox.py).

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
# Create a new inbox item
horizon inbox create --kind hint --title "Use Mathlib lemma X" --body "Detailed explanation..."

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

Protections are persistent inbox items rendered directly into the Horizon agent's prompt context as non-negotiable constraints. Enforcement of write-set boundaries is handled by [`core/freeze.py`](../../src/archon_horizon/core/freeze.py).

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
