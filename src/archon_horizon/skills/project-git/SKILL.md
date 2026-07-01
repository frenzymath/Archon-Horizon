---
name: project-git
description: How git works in an Archon Horizon workspace — root commits include scoped project files plus shared state, while each project also has an out-of-tree VCS journal for detailed per-round history.
---

Read this before running `git diff`/`git log` on a project.

## The model

- **Workspace repo is out-of-tree too** — its git dir is
  `.archon-horizon/vcs/workspace.git` with the workspace **root** as its work tree
  (driven via `--git-dir`/`--work-tree`). **`<root>/.git`** is deliberately not touched, 
  because it is reserved for a user repo. It commits `config.yaml`, scoped project files, and `.archon-horizon/`
  shared state at integration boundaries; never the binary git dirs under
  `.archon-horizon/vcs/`, locks, or other volatile internals.
- **Project repos live out-of-tree.** Every project should have an out-of-tree
  git directory at `.archon-horizon/vcs/<project>.git`, with the project
  directory as its work tree. Project directories do not contain nested `.git/`.
- Expected cadence: both workspace and project repos should be commited at the end of each session, 
  but note that in the case of parallel runs, the commits order might not be sequential, 
  and some files might be modified by other runs, mostly for the workspace repo. However, 
  this should not be a problem in general. 

So a plain `git diff` at the root sees only a user repo (if any), never Archon's.
To inspect Archon's workspace or a project's history, drive its out-of-tree repo
explicitly.

```bash
# Workspace (manifest) history:
git --git-dir=.archon-horizon/vcs/workspace.git --work-tree=. log --oneline -n 20
```

## Reading a project's history / diff

Run from the workspace root:

```bash
GD=.archon-horizon/vcs/<project>.git
WT=<path-to-project>      # the project's `path` from the project list

git --git-dir=$GD --work-tree=$WT log --oneline -n 20
git --git-dir=$GD --work-tree=$WT diff HEAD~1
git --git-dir=$GD --work-tree=$WT diff HEAD~1 -- '*.lean'
git --git-dir=$GD --work-tree=$WT show <sha>
```

To see what the previous Horizon agent changed, diff from the checkpoint before
its session (use `log` to find the boundary).

## Caveats

- Every configured project is expected to have VCS enabled. If
  `.archon-horizon/vcs/<project>.git` is missing, report it as a workspace setup
  issue rather than treating it as normal, but work with it, it might be a new project, or a project that was cloned from the workspace's repo (hence without the vcs committed).
- You read history; you do not commit manually. Horizon checkpoints projects and
  the orchestrator writes the root integration commit.
- The bundled author identity is `Archon Horizon <archon-horizon@local>`.
