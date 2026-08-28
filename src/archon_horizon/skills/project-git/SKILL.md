---
name: project-git
description: How git works in an Archon Horizon workspace — one out-of-tree workspace ledger (no per-project repos); read a project's history by pathspec or by a session's commit trailers.
---

Read this before running `git diff`/`git log` on a project.

## The model

- **One out-of-tree workspace ledger.** Archon commits to a single repo whose git
  dir is `.archon-horizon/vcs/workspace.git` with the workspace **root** as its
  work tree (driven via `--git-dir`/`--work-tree`). `<root>/.git` is deliberately
  untouched — it is reserved for a **user / GitHub publish** repo. The ledger is
  the **agent source journal**: Lean, blueprints, and `config.yaml` — not the
  live Horizon control plane (`.archon-horizon/` inbox/tasks/runs/roadmap) and
  not `**/hgraph/` (regenerable via `horizon graph sync`). Those stay on disk for
  the live dashboard; put anything you want versioned for humans in your own
  root `.git`. Public dashboard snapshots are a separate
  `horizon dashboard --static` step into the user repo.
- **Retro-clean an existing ledger** that still tracks state/hgraph:
  `horizon ledger status` then `horizon ledger prune` (add `--gc` when idle to
  reclaim pack space). Working-tree files are untouched.
- **There are no per-project repositories.** A project directory has no nested
  `.git/`, and there is no `.archon-horizon/vcs/<project>.git`. A project's
  "history" is simply the workspace ledger filtered to that project's path.
- **You record your own work by committing to the ledger** with plain `git` (see
  below). Commit after each independently useful, verified proof/file/tooling
  change, before a long build or delegated review, and before expanding scope.
  A multi-hour implementation session should normally have several commits; one
  commit is appropriate only when the work is genuinely atomic. The message +
  diff is how progress is read, so use a semantic, math-first subject. Provenance
  trailers are added automatically; do not write them.

So a plain `git diff` at the root sees only a user repo (if any), never Archon's.
Drive the out-of-tree workspace ledger explicitly (or use `hgit`).

## Recording your work (committing)

Commits go to the ledger, **not** to `<root>/.git` and **not** via any project
`.git` (there is none). Do **not** `export GIT_DIR` / `GIT_WORK_TREE` yourself — that
would redirect `lake` and other tooling. Two equivalent ways to commit:

```bash
# 1) The `hgit` wrapper (on the session env as $HORIZON_GIT) — plain git, ledger-scoped:
"$HORIZON_GIT" add path/to/File.lean
"$HORIZON_GIT" commit -m "feat(chapter): prove foo_lemma" \
  --trailer 'Summary=**Proof.** Closed the compactness step using $K \subseteq X$.'

# 2) The explicit form (identical effect):
git --git-dir="$HORIZON_LEDGER_GIT_DIR" --work-tree="$HORIZON_LEDGER_WORK_TREE" \
    add path/to/File.lean
git --git-dir="$HORIZON_LEDGER_GIT_DIR" --work-tree="$HORIZON_LEDGER_WORK_TREE" \
    commit -m "feat(chapter): prove foo_lemma" \
    --trailer 'Summary=**Proof.** Closed the compactness step using $K \subseteq X$.'
```

Ordinary git: the ledger's index tracks HEAD, so `add` then `commit` records your
files on top of it. Horizon's own commits stage in a private index and never leave
anything staged in your way.

- **Prefer the pathspec commit** — name the paths on the commit itself:
  `"$HORIZON_GIT" commit -m "…" --trailer 'Summary=…' -- path/to/File.lean`
  It records exactly those files, ignores whatever else may be staged, and git
  builds its tree from the **current** HEAD — so it cannot silently revert a
  concurrent session's files the way a commit built on a stale index can.
- Add at least one **`Summary` trailer** explaining the important change, not
  merely repeating the subject. Use one trailer per distinct explanation when
  useful. Its value supports Markdown and LaTeX, so concise mathematical context
  such as `--trailer 'Summary=**Invariant.** Preserves $f(x)=x$ on $A$.'` renders
  directly in the dashboard commit item. Keep each trailer on one line; repeat
  `--trailer 'Summary=…'` for a few separate points.
- Do not wait for the whole task to be complete. A checkpoint commit must build
  or otherwise be internally coherent, but it may explicitly leave the next
  named obligation open.
- Stage **explicit paths** you changed rather than `add -A` — the work tree is the
  whole workspace, so `-A` can sweep in unrelated files.
- `Archon-Run`/`Session`/`Role`/`Task`/`Projects` trailers are stamped for you by a
  hook from the session env; your message stays clean.
- Build artifacts (`.lake`, `*.olean`, …) are excluded automatically, and any
  high-confidence credential is **redacted to `XXXX`** in the staged content (not
  blocked), so a broad add still won't leak a secret into the ledger.
- If the guard reports *"redacted possible secret(s) to XXXX"*, it scrubbed a
  matched credential and let the commit through — **rotate the real credential**,
  and check you didn't stage more than you meant to (`ARCHON_HORIZON_ALLOW_SECRETS=1`
  skips the scan entirely). A *separate* guard still **blocks** a commit that would
  silently delete tracked files you didn't change (a stale-index clobber); re-seed
  the index (`git read-tree HEAD && git add -- <your files>`) rather than forcing it.

Then verify **in the ledger**, and verify the **paths**, not just the SHA — a bare
`git cat-file` runs against `<root>/.git`, which has not seen your commit yet and will
make a perfectly real SHA look fabricated:

```bash
"$HORIZON_GIT" ls-tree --name-only HEAD -- path/to/File.lean   # empty ⇒ NOT in HEAD
```

## Concurrent sessions and the clobber guard

Several sessions may commit to the ledger at once. A commit whose index was
seeded from an older HEAD (e.g. `read-tree`, then a concurrent session commits,
then you commit) produces a tree that *lacks* the other session's new files —
plain git would commit it without any error and silently delete their work.
The ledger's `pre-commit` guard now rejects any commit that stages deletions
you did not ask for:

- *"this commit would DELETE N tracked file(s) you did not change"* — your
  index base is stale (or you are committing through a polluted shared index).
  **Do not override.** Re-seed and retry; your own edits are safe in the
  working tree, which concurrent commits never touch:

  ```bash
  "$HORIZON_GIT" read-tree HEAD
  "$HORIZON_GIT" add -- <your files>
  "$HORIZON_GIT" commit -m "…"          # or just: commit -m "…" -- <your files>
  ```

- Deleting a file **on purpose** (moved/retired): set
  `ARCHON_HORIZON_ALLOW_DELETIONS=1` for that one commit.

At the **end of the session**, re-verify content — not just that HEAD moved —
since a stale-based commit elsewhere could have landed after yours (the guard
blocks new offenders, but belt-and-braces):

```bash
"$HORIZON_GIT" cat-file -e HEAD:path/to/File.lean && \
"$HORIZON_GIT" show HEAD:path/to/File.lean | diff -q - path/to/File.lean
```

If you were clobbered anyway: `read-tree HEAD`, re-add your files from the
working tree, commit once.

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
