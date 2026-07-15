---
name: horizon-commit
description: Record your own work in the workspace ledger with semantic commits via `horizon commit` — small, meaningful, math-first messages; safe under concurrent runs.
---

Commit your work yourself, semantically, as you finish coherent pieces — don't
leave one giant undifferentiated snapshot for the system to sweep up. Good
commits are how the dashboard's change view and a human reviewer understand what
each session actually did.

## How

Use the Horizon CLI (never raw `git` on the ledger — the workspace repo is
out-of-tree with excludes, a secret guard, and provenance trailers that a bare
`git` would bypass):

- `"$HORIZON_BIN" commit -m "<message>" <files…>` — commit the files you changed.
  Paths are relative to your current directory (your project dir) or absolute.
- `"$HORIZON_BIN" commit -m "<message>" --changed` — commit everything you
  changed in the project (excludes the shared Horizon state, which the system
  commits separately).

Provenance (run / session / role / task / projects) is stamped automatically —
do **not** pass it. The commit is attributed to your role.

## Messages — math first

Write the message the way you'd tell a mathematician what you did: the result
and the idea, in human-readable prose with LaTeX where it helps; mention Lean
declaration names / files only as supporting detail. Mirror the house style,
e.g. `Prove H^i(X,F)=0 for i>dim X via the Čech-to-derived comparison`. One
coherent step per commit; split unrelated work into separate commits.

## Concurrency — you are not alone

Other runs may be committing to the **same** ledger at the same time. That's
fine and intended (it helps everyone converge faster):

- Commits are a single shared branch — they serialise and never conflict as a
  git *merge*; if `horizon commit` hits contention it already retried, so trust
  its result.
- Commit only the files **you** changed. On a shared worktree your commit can
  still capture another run's in-flight edit to the same file — that's expected
  and acceptable; the message + trailers still record your intent.
- Never `reset`, `rebase`, force-push, or otherwise rewrite the ledger. `horizon
  commit` only ever appends a commit.

## When

Commit at each coherent milestone (a lemma closed, a file completed), not only
at the end. Anything you don't commit is still captured by the system's
end-of-session sweep, but as an unlabelled blob — so commit it yourself to keep
the history legible.
