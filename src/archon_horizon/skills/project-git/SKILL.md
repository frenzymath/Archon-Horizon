---
name: project-git
description: How git works in an Archon Horizon workspace — one out-of-tree workspace ledger (no per-project repos); read a project's history by pathspec or by a session's commit trailers.
---

Read this before running `git diff`/`git log` on a project.

## The model

- **One out-of-tree workspace ledger.** Archon commits to a single repo whose git
  dir is `.archon-horizon/vcs/workspace.git` with the workspace **root** as its
  work tree (driven via `--git-dir`/`--work-tree`). `<root>/.git` is deliberately
  untouched — it is reserved for a user repo. The ledger records one commit per
  completed session: `config.yaml`, the session's scoped project files, and
  `.archon-horizon/` shared state; never the binary git dirs under
  `.archon-horizon/vcs/`, locks, or other volatile internals.
- **There are no per-project repositories.** A project directory has no nested
  `.git/`, and there is no `.archon-horizon/vcs/<project>.git`. A project's
  "history" is simply the workspace ledger filtered to that project's path.
- **You never commit.** The orchestrator writes the integration commit for each
  session automatically. You *read* history; you do not commit project or
  workspace repos manually, and there is no "project VCS" to reconcile.

So a plain `git diff` at the root sees only a user repo (if any), never Archon's.
To inspect Archon's history, drive the out-of-tree workspace ledger explicitly.

## Reading history / a project's diff

Run from the workspace root:

```bash
GD=.archon-horizon/vcs/workspace.git

git --git-dir=$GD --work-tree=. log  --oneline -n 20                       # whole ledger
git --git-dir=$GD --work-tree=. log  --oneline -n 20 -- <project-path>     # one project
git --git-dir=$GD --work-tree=. diff HEAD~1 -- <project-path>
git --git-dir=$GD --work-tree=. diff HEAD~1 -- '<project-path>/*.lean'
git --git-dir=$GD --work-tree=. show <sha>
```

Each commit carries provenance trailers — `Archon-Run`, `Archon-Session`,
`Archon-Role`, `Archon-Task`, `Archon-Projects` — so you can map a commit to the
run/session that made it and see what it touched without parsing prose:

```bash
git --git-dir=$GD --work-tree=. \
  log --format='%H %(trailers:key=Archon-Session,valueonly)' -- <project-path>
```

To see what the previous Horizon agent changed, find its session's commit(s) by
the `Archon-Session` trailer and diff each against **its own parent** (what that
commit changed).

## Caveats

- With parallel runs the ledger is one shared branch: commits from several runs
  interleave, and a commit's parent may belong to another run or a dashboard
  publish. Diff a commit against its own parent (what *that* commit changed), not
  across a span of sessions — that would fold in unrelated runs' changes.
- The bundled author identity is `Archon Horizon <archon-horizon@local>`.
