# Contributing to Archon Horizon

Thanks for your interest in improving Archon Horizon. This guide is mostly about
**how to write code that fits the project**, the conventions the existing code
follows. Process (versioning,
releases, issues) is at the end.

## Development setup

```bash
git clone https://github.com/frenzymath/Archon-Horizon.git
cd Archon-Horizon
python -m pip install -e ".[dev]"     # requires-python >= 3.11
```

Run the tests (no install needed — `pyproject.toml` sets `pythonpath = ["src"]`):

```bash
python -B -m pytest -q
```

Tests are smoke/contract style under [`tests/`](./tests); add one next to the
behavior you change. A change should keep the whole suite green.

Enable the versioned git hooks once per clone:

```bash
git config core.hooksPath .githooks
```

The `commit-msg` hook strips AI-assistant attribution (`Co-authored-by:` trailers
naming Claude/Anthropic, "Generated with Claude Code" lines) so it never lands in
history.

---

## Code conventions

### Layering

The dependency direction is one-way: **`core` → stores/inboxes/harnesses →
commands → `cli`**. Nothing lower reaches up.

- **`cli.py`** owns the Typer app and command *registration* only — no behavior.
- **`commands/<name>.py`** owns that command's CLI options and delegates to a
  small command class (e.g. `RunCommand`) or a library service. Keep argument
  parsing out of the library layer.
- **`core/`** holds pure contracts (dataclasses, enums, domain logic) with **no
  I/O**. Anything that touches disk, the network, git, or a subprocess lives in
  `store/`, `inboxes/`, `harnesses/`, `vcs/`, or a command module — never in
  `core/`. If you're tempted to `open()` a file in `core/`, it belongs elsewhere.

### CLI output & the `--json` contract

- **Every command supports `--json`** and emits *pure* JSON on stdout; all human
  chrome (the banner, progress, tables) goes to stderr. The root callback routes
  logging to stderr when `--json` is set — see `cli.py`.
- **Never `print()` to stdout** in a command path. Use `archon_horizon.log` for
  human output and `emit_json(...)` (in `commands/shared.py`) for machine output.
- Commands read the workspace root from `ctx.obj["root"]`; don't re-resolve it.

### Types & dataclasses

- Start every module with `from __future__ import annotations`.
- Model contracts and config as **frozen, slotted dataclasses**
  (`@dataclass(frozen=True, slots=True)`) — see `config/schema.py`,
  `core/`. Parse external/YAML data through a classmethod (`from_raw`) that
  validates and applies defaults, so the rest of the code sees typed objects.
- Prefer explicit types over `Any`; `tuple[...]` for immutable sequences on
  contracts.

### Defensive by default; never crash a run

- **Parsers degrade, they don't raise.** A line that doesn't match a known shape
  yields nothing rather than throwing (`transcript/parsers.py`), so an engine
  version bump degrades gracefully. Per-engine format knowledge lives *only* in
  the parsers — don't leak it elsewhere.
- **Optional/best-effort steps are wrapped** so they can't abort a run: subagent
  compilation, transcript ingestion, and log materialization catch their own
  exceptions and emit an `ERROR` event or `log.warn` instead of propagating (see
  `harnesses/command.py`, `commands/run.py`).
- **Degrade when a tool is absent** rather than failing hard: `git_available()`
  is `False` but constructors still build; a missing engine binary becomes a
  clear `HarnessResult(ok=False, ...)`, not a traceback.
- Classify failures you can act on (rate limit vs. usage limit vs. genuine
  error) and retry only the transient ones — see the retry/classification in
  `harnesses/command.py`.

### The engine seam

Engines plug in behind the `Harness` ABC (`harnesses/base.py`). **Adding an
engine is a new argv + a new parser, not a new code path.** Keep engine-specific
details confined to: the argv/env builder (`config/harnesses.py`), the native
log parser (`transcript/parsers.py`), and the per-engine descriptor compiler
(`subagents/compile.py`). Orchestration stays engine-agnostic.

### Managed files: marker-guarded & idempotent

Files Horizon writes into a workspace (skills, native subagents, MCP config, git
excludes) are **regenerated on every run / `horizon init --reinit`**, so they
self-heal — but they must **never silently clobber a user's edits**. Follow the
existing patterns:

- Write only files carrying our generated-marker; leave a hand-authored file with
  the same name alone (`_write_if_ours` in `subagents/compile.py`).
- On reinit, preserve local content and prompt before overwriting where relevant
  (`_sync_managed_file` in `commands/init.py`).
- Make the operation idempotent: re-running produces the same result and repairs
  drift (e.g. `_ensure_repo_hygiene` rewrites git excludes/hooks each init).

### Version control model

Horizon keeps history **out-of-tree** and never creates a `.git` at a workspace
or project root. Excludes live in each git dir's `info/exclude` (not a
`.gitignore`); new repos are pinned to the `main` branch **at creation only** and
a user's manual branch switch is respected (nothing checks out/resets/pushes).
Keep this model intact — see `vcs/git.py`.

### Comments & docstrings

Match the surrounding density, which is high and explains the **why**, not the
what. Module docstrings state the design intent; inline comments justify a
non-obvious decision (a workaround, an ordering constraint, a footgun avoided).
If a reviewer would ask "why is this here?", answer it in a comment.

### Backward compatibility

While `0.x`, keep changes backward-compatible where you can. If a change alters
an on-disk format, make `horizon init --reinit` migrate it and note it in
`docs/CHANGELOG.md`. General, modular `class`-based structure is preferred over
one-off scripts.

---

## Versioning & releases

- The version lives in **one place**: `archon_horizon.__version__`
  (`src/archon_horizon/__init__.py`); `pyproject.toml` reads it dynamically. Do
  not duplicate it.
- Semantic Versioning; while `0.x`, minor releases may change on-disk formats
  (document migrations in `docs/CHANGELOG.md`).
- To cut a release: bump `__version__`, move `docs/CHANGELOG.md` `[Unreleased]`
  entries under a dated heading, update the README version badge, run the tests,
  then commit and tag `vX.Y.Z`. `install.sh` pulls the `main` tarball, so a
  release is live on `main` immediately; the tag is for provenance.

## Reporting issues

Open an issue at <https://github.com/frenzymath/Archon-Horizon/issues> with your
`horizon --version`, the command you ran, and the relevant `--json` output or
logs.
