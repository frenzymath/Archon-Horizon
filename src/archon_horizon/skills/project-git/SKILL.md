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
- **You record your own work by committing to the ledger** with plain `git` (see
  below). Commit *early and often* with a semantic, math-first message — the
  commit message + diff is how your progress is read, so make the message say what
  you proved/built. Provenance trailers are added automatically; do not write them.

So a plain `git diff` at the root sees only a user repo (if any), never Archon's.
Drive the out-of-tree workspace ledger explicitly (or use `hgit`).

## Recording your work (committing)

Commits go to the ledger, **not** to `<root>/.git` and **not** via any project
`.git` (there is none). Do **not** `export GIT_DIR` / `GIT_WORK_TREE` yourself — that
would redirect `lake` and other tooling. Two equivalent ways to commit:

```bash
# 1) The `hgit` wrapper (on the session env as $HORIZON_GIT) — plain git, ledger-scoped:
"$HORIZON_GIT" add path/to/File.lean
"$HORIZON_GIT" commit -m "feat(chapter): prove foo_lemma"

# 2) The explicit form (identical effect):
git --git-dir="$HORIZON_LEDGER_GIT_DIR" --work-tree="$HORIZON_LEDGER_WORK_TREE" \
    add path/to/File.lean
git --git-dir="$HORIZON_LEDGER_GIT_DIR" --work-tree="$HORIZON_LEDGER_WORK_TREE" \
    commit -m "feat(chapter): prove foo_lemma"
```

- Prefer staging **explicit paths** you changed over `add -A` — the work tree is the
  whole workspace, so `-A` can sweep in unrelated files.
- `Archon-Run`/`Session`/`Role`/`Task`/`Projects` trailers are stamped for you by a
  hook from the session env; your message stays clean.
- Build artifacts (`.lake`, `*.olean`, …) and secrets are excluded/blocked
  automatically, so a broad add still won't commit them.

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
