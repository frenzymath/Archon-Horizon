"""Typer-decorated ``init`` entry point."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import typer

from archon_horizon.config.env import write_env_template
from archon_horizon.core.scope import ItemScope
from archon_horizon.log import log

_CONFIG_TEMPLATE = """\
workspace:
  name: {name}
  state_dir: .archon-horizon
  # Maximum number of collaboration rounds the orchestrator will allow before halting.
  rounds: {rounds}
  ground_agent:
    harness: ground-default
  horizon_agent:
    harness: horizon-default
  scheduler:
    # How many horizon agents can run simultaneously.
    max_parallel_sessions: {parallel}

# Lean libraries the agents and the `horizon search` index should know about.
# mathlib is included by default. Add more with a GitHub shorthand or a full git
# url; `name` alone works for well-known libraries.
external_libraries:
  - name: mathlib
    rev: {mathlib_version}
  # - name: my-lib            # clones https://github.com/owner/my-lib by default
  #   github: owner/my-lib
  #   rev: main
  # - name: vendored          # already checked out under the workspace
  #   path: vendor/vendored
  #   git: https://example.com/vendored.git

references:
  transcription:
    # Page-level PDF transcription defaults to the Ground subagent harness.
    # Override both fields to pin a cheap vision-capable model.
    # harness: ground-default
    # model: <vision-capable-model>

harnesses:
  ground-default:
    kind: "{ground_kind}"
    model: {ground_model}{ground_options}
  horizon-default:
    kind: "{horizon_kind}"
    model: {horizon_model}{horizon_options}

github:
  # Enable if you want to sync GitHub issues directly into your inbox.
  enabled: {github_enabled}
  repo: {github_repo}

projects: {{}}
"""

# Legacy starter descriptors older Horizon versions seeded verbatim into
# ``.archon-horizon/subagents/``. The canonical roster now lives in the bundled
# ``archon_horizon/subagents/descriptors/`` package dir (always merged in at
# compile/catalog time), so these workspace copies are stale duplicates that
# only pollute the roster. ``--update`` removes them (see the migration below).
# Removal is by *name*: these names are reserved for the (now bundled) starter
# roster, so any ``<name>.md`` still sitting in the workspace is a stale seed. If
# you hand-authored your own descriptor under one of these names, rename it
# before ``horizon init --update`` or it will be deleted.
_LEGACY_SEEDED_SUBAGENTS: tuple[str, ...] = ("blueprint-reviewer", "diff-auditor")


def _default_model(kind: str) -> str:
    # We return empty strings to defer to the harness's own config.
    return ""


def _options_block(kind: str) -> str:
    """Kind-specific harness ``options:`` YAML (or empty).

    Keeps engine-specific keys off the wrong engine — e.g. Codex's ``sandbox`` must
    not land on a ``claude-code`` harness, and vice-versa. Both engines honour
    ``effort`` (Codex passes it as ``model_reasoning_effort``; Claude Code passes
    it as the native ``--effort`` flag, or a raw integer as a ``MAX_THINKING_TOKENS``
    budget). The returned string is spliced right after the ``model:`` line, so it
    starts with a newline and has no trailing newline."""
    if kind == "codex":
        return (
            "\n    options:\n"
            "      # Reasoning effort — 'default' leaves Codex's own default.\n"
            "      # default | low | medium | high\n"
            "      effort: default"
        )
    if kind == "claude-code":
        return (
            "\n    options:\n"
            "      # Effort → claude's native --effort flag — 'default' leaves Claude Code's own.\n"
            "      # default | low | medium | high | xhigh | max   (or a raw integer = MAX_THINKING_TOKENS)\n"
            "      # 'ultracode' = xhigh + automatic dynamic-workflow orchestration (via --settings;\n"
            "      # needs workflows enabled + claude >= 2.1.154; can fan out to many agents per task).\n"
            "      effort: default\n"
            "      # backend: default   # default | vscode | desktop | claude-p"
        )
    return ""


def _model_help(kind: str) -> list[str]:
    """Guidance for the model prompt of a given harness.

    Rather than hard-coding model names (which go stale), point at where to find
    the currently-accepted ones for each CLI. Leaving the prompt blank defers to
    the harness's own default.
    """
    if kind == "claude-code":
        return [
            "Accepts a Claude alias (opus/sonnet/haiku) or a full model ID.",
            "List what your install accepts with `claude --help` or `/model` in a session; current IDs are in the Claude Code docs (docs.claude.com).",
            "Other providers also work via Claude Code: Kimi/DeepSeek with their API key + ANTHROPIC_BASE_URL, or many more with an OpenRouter key — see each provider's docs for model names.",
            "Leave blank to use the Claude Code default.",
        ]
    if kind == "codex":
        return [
            "See accepted models with `codex --help` (the -m/--model flag) or the OpenAI model docs.",
            "Leave blank to use the Codex default.",
        ]
    return ["Leave blank to use the harness default."]

def _binary_for_kind(kind: str) -> str | None:
    if kind == "claude-code": return "claude"
    if kind == "codex": return "codex"
    return None


def _detect_github_repo() -> str:
    import subprocess
    try:
        out = subprocess.check_output(["git", "config", "--get", "remote.origin.url"], text=True, stderr=subprocess.DEVNULL).strip()
        import re
        m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def _find_mathlib_revs(root: Path) -> dict[str, set[str]]:
    """Map each project dir to the mathlib rev identifiers its manifest pins.

    Each value holds both the resolved ``rev`` (SHA) and the ``inputRev``
    (tag/branch) — see ``find_package_revs``."""
    from archon_horizon.config.manifest import find_package_revs

    return find_package_revs(root, "mathlib")


def _looks_like_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value.lower())


def _pick_readable(ids: set[str]) -> str:
    """A representative rev, preferring a human tag/branch over a raw SHA."""
    tags = sorted(v for v in ids if not _looks_like_sha(v))
    return tags[0] if tags else sorted(ids)[0]


def _fetch_latest_mathlib_tag() -> str:
    """Best-effort latest mathlib4 release tag from GitHub, e.g. 'v4.15.0' ('' on failure).

    A version tag is far more human-readable than a commit SHA, so we prefer it
    for the network fallback; projects that pin a raw rev still take precedence.
    """
    import json
    import re
    import urllib.request

    req = urllib.request.Request(
        "https://api.github.com/repos/leanprover-community/mathlib4/tags?per_page=50",
        headers={"Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return ""
    names = [t.get("name", "") for t in tags if isinstance(t, dict)]
    versioned = [n for n in names if re.match(r"^v\d+\.\d+", n)]
    if versioned:
        return max(versioned, key=lambda n: tuple(int(x) for x in re.findall(r"\d+", n)))
    return names[0] if names else ""


def _detect_mathlib_version(root: Path) -> str:
    """Propose a Mathlib version: detected from projects, else latest tag, else master."""
    picks = [_pick_readable(ids) for ids in _find_mathlib_revs(root).values() if ids]
    if picks:
        return max(set(picks), key=picks.count)
    return _fetch_latest_mathlib_tag() or "master"


def _warn_mathlib_mismatch(root: Path, chosen: str) -> None:
    """Warn if any project pins a Mathlib rev different from the chosen one.

    ``chosen`` matches a project when it equals *either* its ``rev`` or its
    ``inputRev``, so a chosen tag agrees with the SHA it resolves to."""
    mismatched = {d: ids for d, ids in _find_mathlib_revs(root).items() if chosen not in ids}
    if not mismatched:
        return
    log.warn(f"Some projects pin a different Mathlib version than {chosen!r}:")
    for project_dir, ids in mismatched.items():
        log.step(f"{project_dir}: {_pick_readable(ids)}")


def _print_config_summary(data: dict) -> None:
    from rich.table import Table
    from rich.console import Console
    table = Table(title="Workspace Configuration")
    table.add_column("Property", style="#0ea5e9")
    table.add_column("Value", style="#8b5cf6")
    table.add_row("Workspace Name", str(data.get("name", "")))
    
    ground_model = data.get("ground_model") or _default_model(str(data.get("ground_kind", "")))
    table.add_row("Ground Agent", f"{data.get('ground_kind', '')} ({ground_model})")
    
    horizon_model = data.get("horizon_model") or _default_model(str(data.get("horizon_kind", "")))
    table.add_row("Horizon Agent", f"{data.get('horizon_kind', '')} ({horizon_model})")
    
    table.add_row("Mathlib Version", str(data.get("mathlib_version", "")))
    table.add_row("Rounds", str(data.get("rounds", "")))
    table.add_row("Max Parallel", str(data.get("parallel", "")))
    
    is_gh_enabled = str(data.get("github_enabled", "")).lower() == "true"
    table.add_row("GitHub Sync", "Enabled" if is_gh_enabled else "Disabled")
    if is_gh_enabled:
        table.add_row("GitHub Repo", str(data.get("github_repo", "")))
    
    Console().print(table)


def _post_init_advisor_prompt(root: Path) -> str:
    return dedent(
        f"""\
        You are the Archon Horizon post-init interactive workspace advisor.

        Workspace root:
        {root}

        Your role is to help the user understand Archon Horizon, choose a good configuration for their
        actual workflow, and clean up the workspace created by `horizon init`. This is an interactive
        advisory session, not an autonomous maintenance run.

        Hard rule: ask for the user's explicit agreement before changing files, renaming anything,
        enabling/disabling features, adding tasks, rewriting dependencies, or running commands that
        mutate the workspace or remote services. Explain what each proposed change does and why.

        Do not rely on this prompt as the full specification. First infer the current ideal workspace
        from the installed Archon Horizon package and the local workspace files.

        Start by discovering the package source and reading the relevant implementation:

        ```bash
        python - <<'PY'
        import inspect
        import pathlib
        import archon_horizon
        print(pathlib.Path(inspect.getfile(archon_horizon)).resolve().parent)
        PY
        ```

        Prefer the current source code over any stale advice in this prompt. In particular, inspect
        the config schema/loader, init/setup/project/run commands, orchestrator wiring, harness
        configuration, permissions/freeze handling, subagent descriptors, skills/tool installation,
        dashboard/export expectations, and any README or docs shipped with the package.

        Help the user with at least these areas:

        - `config.yaml`: workspace name, state dir, Ground/Horizon harness references, scheduler,
          external_libraries (mathlib + any other Lean deps to index/search), GitHub settings,
          freeze rules, project declarations, project dependencies,
          write paths, build commands, blueprint paths, harness options, and any disabled feature that
          might have been accidental.
        - `.archon-horizon/`: expected stores and directories, inbox/tasks/runs/blueprints,
          subagent wrappers, tools, skills, VCS state, and volatile files that should stay run-local.
        - Lean/mathlib setup: `lean-toolchain`, `lakefile.lean`, `lake-manifest.json`, Mathlib revision
          consistency, root package sharing, project imports, build commands, and dependency layout.
        - Blueprint setup: blueprint files, links between blueprint nodes, Lean declarations and graph
          nodes, paths configured per project, and obvious stale or missing references.
        - Git/workspace hygiene: repository status, `.gitignore`, nested project VCS settings,
          remotes/branches where relevant, generated files, and files that should or should not be tracked.
        - Environment/tooling: `.env`, `.env.example`, `.mcp.json`, required external binaries,
          provider keys, command harnesses, MCP servers, `gh` availability/authentication/repo access,
          and setup steps required before `horizon run`.
        - Model and backend choices: explain tradeoffs, flag weak or mismatched model choices, and propose
          smaller/cheaper models for simple Horizon or Ground tasks when appropriate.
        - Operational state: useful initial inbox items, tasks, concise roadmap recommendations, project metadata,
          and concrete next commands the user should run.

        Explain Archon Horizon in practical terms when useful: what the Ground agent does, what Horizon
        agents do, how inbox/tasks/run-local reports fit together, how projects and blueprints are wired,
        and how the user should work with the system day to day.

        Suggested flow:

        1. Briefly explain what you will inspect and ask what the user wants Archon Horizon to manage.
        2. Inspect the package source and workspace configuration.
        3. Summarize findings in priority order with exact paths.
        4. Ask targeted questions about ambiguous choices, especially disabled features, naming, GitHub,
           model/backend choices, project layout, and Lean/mathlib expectations.
        5. Propose a small batch of safe edits. Wait for approval before applying them.
        6. For risky changes, explain tradeoffs and leave the decision to the user.
        """
    ).strip()


def _write_post_init_advisor_prompt(root: Path, text: str) -> Path:
    """Persist the advisor prompt as a *fallback* artifact (only used when the
    advisor can't be launched). It lives under ``runs/`` because this is a
    run/setup artifact, and the prompt is normally passed straight to the
    interactive advisor."""
    prompt_dir = root / ".archon-horizon" / "runs" / "post-init-advisor"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompt_dir / "prompt.md"
    prompt_path.write_text(f"# Post-init Advisor Prompt\n\n{text.rstrip()}\n", "utf-8")
    return prompt_path


# The interactive-launch helpers now live in ``commands/interactive.py`` (shared
# by the post-init advisor, ``horizon discuss``, and ``horizon run <role>
# --backend interactive``).


def _launch_post_init_advisor(root: Path) -> None:
    from .interactive import interactive_launch_for_role, run_interactive

    log.header("Post-init workspace advisor")
    prompt = _post_init_advisor_prompt(root.resolve())
    try:
        launch = interactive_launch_for_role(root, "ground", prompt)
    except Exception as exc:
        prompt_path = _write_post_init_advisor_prompt(root, prompt)
        log.warn(f"Could not launch the interactive advisor: {exc}")
        log.info(f"The advisor prompt was saved to {prompt_path}.")
        return

    if launch is None:
        prompt_path = _write_post_init_advisor_prompt(root, prompt)
        log.info(f"Ground harness is 'null'; advisor prompt saved to {prompt_path}.")
        return

    # The prompt is passed directly to the Ground agent — no file is written
    # in the normal path.
    log.info(f"Launching an interactive workspace advisor using {launch.description}.")
    run_interactive(launch, root)


class InitCommand:
    def __init__(
        self,
        root: Path,
        *,
        config_json: str | None = None,
        interactive: bool = True,
        post_init_advisor: bool = False,
        as_json: bool = False,
        update: bool = False,
    ) -> None:
        self.root = root
        self.config_json = config_json
        # Update/reinit mode: re-run the install steps on an existing workspace to
        # bring its derived, Horizon-managed artifacts (skills, subagent compile,
        # MCP, gitignore) up to date with this Horizon, preserving config and
        # user content. It is a non-prompting refresh.
        self.update = update
        self.interactive = interactive and not update
        self.post_init_advisor = post_init_advisor
        self.as_json = as_json

    def _sync_managed_file(self, dest: Path, content: str, label: str, *, executable: bool = False) -> None:
        """Write a Horizon-managed file, preserving local edits on reinit.

        New files are written outright. An existing file identical to the bundled
        content is left untouched (no prompt). If it differs, we ask to overwrite
        when interactive (default keep), and keep it silently when not — so a
        reinit never clobbers user changes behind their back.
        """
        if dest.exists():
            if dest.read_text("utf-8") == content:
                return
            if self.interactive:
                from rich.prompt import Confirm

                if not Confirm.ask(f"{label} has local changes. Overwrite with the bundled version?", default=False):
                    return
            else:
                return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, "utf-8")
        if executable:
            dest.chmod(0o755)
        log.success(f"Wrote {label}.")

    def _interactive_workspace_setup(self) -> None:
        """Pedagogical, optional loops to populate projects, tasks, and hints."""
        import yaml
        from rich.prompt import Confirm, Prompt

        from archon_horizon.config import operations

        log.header("Projects")
        log.step("A project is a Lean package the agents work in — its own folder, git history, and build.")
        while Confirm.ask("Add a project?", default=False):
            name = Prompt.ask("  Project name").strip()
            if not name:
                continue
            path = Prompt.ask("  Path (relative to the workspace)", default=f"projects/{name}")
            build = Prompt.ask("  Build command", default="lake build")
            try:
                operations.add_project(self.root, name, path, build_command=build or None)
                log.success(f"Added project {name} at {path}.")
            except Exception as exc:
                log.warn(f"Could not add project: {exc}")

        conf = yaml.safe_load((self.root / "config.yaml").read_text("utf-8")) or {}
        project_names = list((conf.get("projects") or {}).keys())

        from archon_horizon.commands.shared import load_workspace, local_inbox
        from archon_horizon.config.loader import build_stores

        _, workspace = load_workspace(self.root)
        stores = build_stores(workspace)

        if project_names:
            log.header("Tasks")
            log.step("A task is a concrete unit of work for a Horizon agent, scoped to one project.")
            from archon_horizon.core.tasks import HorizonTask, WriteSet

            while Confirm.ask("Add a task?", default=False):
                title = Prompt.ask("  Task title").strip()
                if not title:
                    continue
                project = Prompt.ask("  Project", choices=project_names, default=project_names[0])
                explanation = Prompt.ask("  Explanation (optional)", default="")
                task_id = f"T-{len(stores.tasks.list()) + 1:04d}"
                stores.tasks.put(
                    HorizonTask(
                        id=task_id,
                        project=project,
                        objective=explanation or title,
                        title=title,
                        explanation=explanation,
                        projects=(project,),
                        write_set=WriteSet(projects=(project,)),
                        scope=ItemScope(projects=(project,)),
                        metadata={"author": "human"},
                    )
                )
                log.success(f"Added task {task_id}.")

        log.header("Hints")
        log.step("A hint is a free-form note that steers the agents (e.g. 'try the affine case first').")
        log.step("Agents read open hints; you can prune them anytime from the Inbox.")
        inbox = local_inbox(workspace)
        from archon_horizon.core.inbox import InboxDraft, InboxKind, InboxScope
        from archon_horizon.core.labels import AGENT_READY

        while Confirm.ask("Add a hint?", default=False):
            body = Prompt.ask("  Hint").strip()
            if not body:
                continue
            inbox.create_item(InboxDraft(kind=InboxKind.HINT, body=body, author="human"))
            log.success("Added hint.")

        log.header("Protections")
        log.step("A protection is a standing constraint for Horizon, such as a file or declaration it must not modify.")
        log.step("Protections are stored as persistent inbox items so Ground and Horizon both see them.")
        while Confirm.ask("Protect a file, declaration, or convention?", default=False):
            project = ""
            if project_names:
                project = Prompt.ask("  Project scope (blank for workspace-wide)", default="")
                if project and project not in project_names:
                    log.warn(f"Project {project!r} is not in config.yaml; keeping it as a free-form scope.")
            file = Prompt.ask("  File scope (optional)", default="").strip() or None
            declaration = Prompt.ask("  Declaration scope (optional)", default="").strip() or None
            default_body = "do not modify"
            if declaration:
                default_body = f"do not change the signature or statement of {declaration}"
            elif file:
                default_body = f"do not modify {file} without explicit approval"
            body = Prompt.ask("  Protection", default=default_body).strip()
            if not body:
                continue
            inbox.create_item(
                InboxDraft(
                    kind=InboxKind.PROTECTION,
                    body=f"[persistent] {body}",
                    labels=(AGENT_READY,),
                    scope=InboxScope(
                        projects=(project,) if project else (),
                        files=(file,) if file else (),
                        declarations=(declaration,) if declaration else (),
                    ),
                    audience="horizon",
                    author="human",
                )
            )
            log.success("Added protection.")

    def run(self) -> None:
        config_path = self.root / "config.yaml"
        write_config = True
        
        # Defaults
        data = {
            "name": self.root.resolve().name,
            "ground_kind": "claude-code",
            "ground_model": None,
            "horizon_kind": "claude-code",
            "horizon_model": None,
            "mathlib_version": _detect_mathlib_version(self.root),
            "rounds": 5,
            "parallel": 1,
            "github_enabled": "false",
            "github_repo": _detect_github_repo(),
            "goal": "",
        }

        if config_path.exists():
            import yaml
            try:
                old_conf = yaml.safe_load(config_path.read_text("utf-8")) or {}
                if "workspace" in old_conf:
                    ws = old_conf["workspace"]
                    data["name"] = ws.get("name", data["name"])
                    data["rounds"] = ws.get("rounds", data["rounds"])
                    if "scheduler" in ws:
                        data["parallel"] = ws["scheduler"].get("max_parallel_sessions", data["parallel"])
                for lib in old_conf.get("external_libraries") or []:
                    name = lib.get("name") if isinstance(lib, dict) else str(lib).split("@")[0]
                    if name and name.rsplit("/", 1)[-1].lower() in {"mathlib", "mathlib4"}:
                        rev = lib.get("rev") if isinstance(lib, dict) else (str(lib).split("@") + [None])[1]
                        if rev:
                            data["mathlib_version"] = rev
                if "lean" in old_conf and isinstance(old_conf["lean"], dict):
                    # Migrate the removed key so re-init preserves the pinned rev.
                    data["mathlib_version"] = old_conf["lean"].get("mathlib_version", data["mathlib_version"])
                if "harnesses" in old_conf:
                    inf = old_conf["harnesses"].get("ground-default", {})
                    hor = old_conf["harnesses"].get("horizon-default", {})
                    data["ground_kind"] = inf.get("kind", data["ground_kind"])
                    data["ground_model"] = inf.get("model", data["ground_model"])
                    data["horizon_kind"] = hor.get("kind", data["horizon_kind"])
                    data["horizon_model"] = hor.get("model", data["horizon_model"])
                if "github" in old_conf:
                    data["github_enabled"] = str(old_conf["github"].get("enabled", "false")).lower()
                    data["github_repo"] = old_conf["github"].get("repo", data["github_repo"])
            except Exception as exc:
                log.warn(f"Failed to parse existing config.yaml for defaults: {exc}")

            if self.update:
                # Reinit: keep the existing config, just refresh derived artifacts.
                write_config = False
                log.info("Update mode: keeping config.yaml; refreshing skills, subagents, and MCP setup.")
            elif self.interactive:
                from rich.prompt import Confirm
                log.header("Existing configuration found:")
                _print_config_summary(data)
                if not Confirm.ask("config.yaml already exists. Overwrite configuration?", default=False):
                    write_config = False
            elif not self.config_json:
                log.error(
                    f"config.yaml already exists at {config_path}. "
                    "Re-run with `--update` to refresh skills/subagents/MCP for an upgraded Horizon."
                )
                raise typer.Exit(1)
        
        if write_config:
            if self.config_json:
                import json
                try:
                    parsed = json.loads(self.config_json)
                    data.update(parsed)
                    self.interactive = False
                except json.JSONDecodeError as exc:
                    log.error(f"Invalid JSON in --config-json: {exc}")
                    raise typer.Exit(1)
                    
            if self.interactive:
                import shutil
                from rich.prompt import Prompt, Confirm, IntPrompt
                
                data["name"] = Prompt.ask("Workspace name", default=data["name"])
                
                def prompt_harness(label: str, default_kind: str) -> str:
                    while True:
                        kind = Prompt.ask(
                            label,
                            choices=["claude-code", "codex", "command"],
                            default=default_kind
                        )
                        binary = _binary_for_kind(kind)
                        if binary:
                            if shutil.which(binary):
                                log.success(f"Verified binary '{binary}' is installed.")
                            else:
                                log.warn(f"Warning: binary '{binary}' not found in PATH for harness '{kind}'. You may need to run `horizon setup` later.")
                        return kind

                data["ground_kind"] = prompt_harness("Ground agent harness", data["ground_kind"])
                data["horizon_kind"] = prompt_harness("Horizon agent harness", data["horizon_kind"])

                for line in _model_help(data["ground_kind"]):
                    log.step(line)
                data["ground_model"] = Prompt.ask(
                    "Ground agent model",
                    default=data["ground_model"] or _default_model(data["ground_kind"])
                )
                if data["horizon_kind"] != data["ground_kind"]:
                    for line in _model_help(data["horizon_kind"]):
                        log.step(line)
                data["horizon_model"] = Prompt.ask(
                    "Horizon agent model",
                    default=data["horizon_model"] or _default_model(data["horizon_kind"])
                )

                for key, label in [("ground_model", "Ground"), ("horizon_model", "Horizon")]:
                    val = data[key]
                    if val and val != "default-model":
                        log.step(f"Note: Horizon does not verify if '{val}' is a valid {label} model. Typos will cause runtime API errors.")

                log.step("Mathlib rev: detected from your projects, or the latest master if none — keep projects in sync.")
                data["mathlib_version"] = Prompt.ask("Mathlib version/rev", default=data["mathlib_version"])
                _warn_mathlib_mismatch(self.root, data["mathlib_version"])
                data["rounds"] = IntPrompt.ask("Number of collaboration rounds", default=data["rounds"])
                data["parallel"] = IntPrompt.ask("Max parallel horizon sessions", default=data["parallel"])
                
                wants_gh = Confirm.ask("Enable GitHub inbox sync?", default=(data["github_enabled"] == "true"))
                if wants_gh:
                    data["github_enabled"] = "true"
                    data["github_repo"] = Prompt.ask("GitHub repo (owner/repo)", default=data["github_repo"])
                    # GitHub sync goes through the `gh` CLI — verify it's present and
                    # authenticated, pointing at the official install page otherwise.
                    from .shared import github_cli_status
                    gh_status, gh_detail = github_cli_status()
                    (log.success if gh_status == "ok" else log.warn)(gh_detail)
                else:
                    data["github_enabled"] = "false"
                
                data["goal"] = Prompt.ask("Initial project goal (optional)", default=data.get("goal", ""))
            
            # Finalize models if not explicitly provided (e.g. from JSON)
            if not data["ground_model"]:
                data["ground_model"] = _default_model(data["ground_kind"])
            if not data["horizon_model"]:
                data["horizon_model"] = _default_model(data["horizon_kind"])

            # Only emit engine-specific harness options for the matching kind, so
            # Codex's `effort` never leaks onto a claude-code harness (or vice-versa).
            data["ground_options"] = _options_block(str(data["ground_kind"]))
            data["horizon_options"] = _options_block(str(data["horizon_kind"]))

            self.root.mkdir(parents=True, exist_ok=True)
            config_text = _CONFIG_TEMPLATE.format(**data)
            config_path.write_text(config_text, "utf-8")
            
            if not self.as_json:
                log.header("New configuration saved:")
                _print_config_summary(data)

            # Non-interactive GitHub enablement (e.g. --config-json) still needs the
            # `gh` CLI; verify it here since the interactive prompt was skipped.
            if not self.interactive and str(data.get("github_enabled")).lower() == "true" and not self.as_json:
                from .shared import github_cli_status
                gh_status, gh_detail = github_cli_status()
                (log.success if gh_status == "ok" else log.warn)(gh_detail)

            if data.get("goal"):
                from archon_horizon.core.inbox import InboxDraft, InboxKind
                from archon_horizon.core.labels import NOT_READY
                from archon_horizon.inboxes.filesystem import FilesystemInboxProvider

                inbox = FilesystemInboxProvider(self.root / ".archon-horizon" / "inbox" / "local")
                inbox.create_item(
                    InboxDraft(
                        kind=InboxKind.HINT,
                        body=f"Initial goal: {data['goal']}\n\nPlease break this down and plan the work.",
                        labels=(NOT_READY,),
                        author="human",
                    )
                )

        # Initialize Git if needed
        import subprocess
        if not (self.root / ".git").exists():
            subprocess.run(["git", "init", "-b", "main"], cwd=self.root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.success("Initialized empty Git repository on branch 'main'.")
            
        # Update .gitignore. The state directory is the workspace ledger, so
        # keep it trackable while excluding volatile project git dirs and locks.
        gitignore = self.root / ".gitignore"
        lines: list[str] = []
        if gitignore.exists():
            lines = gitignore.read_text("utf-8").splitlines()
        migrated = False
        kept: list[str] = []
        for line in lines:
            if line.strip() in {".archon-horizon", ".archon-horizon/"}:
                migrated = True
                continue
            kept.append(line)
        ignores = set(kept)
        
        added_ignores = False
        # `.claude/agents` and `.codex/agents` are compiled from the tracked
        # subagent descriptors at run start — derived, so don't commit them.
        for entry in (".env", ".archon-horizon/vcs/", ".archon-horizon/locks/", ".claude/agents/", ".codex/agents/"):
            if entry not in ignores:
                kept.append(entry)
                ignores.add(entry)
                added_ignores = True
        if migrated or added_ignores:
            gitignore.write_text("\n".join(kept).rstrip() + "\n", "utf-8")
            log.info("Updated .gitignore for env files and volatile Horizon state.")

        env_path = self.root / ".env"
        env_created = write_env_template(self.root)
        if env_created:
            log.success(f"A {env_path.name} file was created. Fill in any API keys needed before `horizon run`.")
        
        for sub in ("blueprints", "inbox/local/items", "inbox/local/comments", "inbox/github/items", "inbox/github/comments", "roadmap/items", "roadmap/comments", "runs", "subagents", "tasks", "tools", "vcs"):
            (self.root / ".archon-horizon" / sub).mkdir(parents=True, exist_ok=True)

        # Stamp the running Horizon version into the workspace so later commands
        # can warn on drift and the user knows which Horizon built this workspace.
        from archon_horizon import __version__
        from archon_horizon.core.version import stamp_workspace_version

        stamp_workspace_version(self.root)
        log.info(f"Stamped workspace with Horizon {__version__}.")

        # The subagent roster is the bundled ``archon_horizon/subagents/descriptors/``
        # package dir — always merged in at compile/catalog time, so Ground has its
        # review/upkeep helpers without anything being seeded into the workspace.
        # ``.archon-horizon/subagents/`` is reserved for the user's own custom
        # descriptors and starts empty. On ``--update`` we also remove the legacy
        # starter descriptors older versions seeded there, which are now stale
        # duplicates that pollute the roster (blueprint-reviewer, diff-auditor).
        sub_dir = self.root / ".archon-horizon" / "subagents"
        if self.update:
            removed = 0
            for name in _LEGACY_SEEDED_SUBAGENTS:
                stale = sub_dir / f"{name}.md"
                if stale.exists():
                    stale.unlink()
                    removed += 1
            if removed:
                log.success(
                    f"Removed {removed} stale legacy subagent descriptor(s) from "
                    ".archon-horizon/subagents/ (superseded by the bundled roster)."
                )

        from archon_horizon.skills.registry import install_skills

        def _skill_overwrite(name: str, dest: Path, new_text: str) -> bool:
            if not self.interactive:
                return False
            from rich.prompt import Confirm

            return Confirm.ask(f"Skill '{name}' has local changes. Overwrite with the bundled version?", default=False)

        # Update mode force-refreshes stale skills to the bundled versions (that is
        # the point of a reinit after upgrading); a fresh/interactive init keeps
        # local edits unless the user confirms overwrite.
        installed = install_skills(self.root, overwrite=None if self.update else _skill_overwrite)
        if installed:
            verb = "Updated" if self.update else "Installed"
            log.success(f"{verb} {len(installed)} skill(s) under .claude/skills/.")

        # Install the editable agent prompt bodies (ground.md/horizon.md). Same
        # keep-vs-overwrite policy as skills: update mode force-refreshes to the
        # bundled versions; a fresh/interactive init keeps local edits unless the
        # user confirms overwrite.
        from archon_horizon.agents.prompts import install_prompts

        def _prompt_overwrite(name: str, dest: Path, new_text: str) -> bool:
            if not self.interactive:
                return False
            from rich.prompt import Confirm

            return Confirm.ask(f"Prompt '{name}' has local changes. Overwrite with the bundled version?", default=False)

        installed_prompts = install_prompts(self.root, overwrite=None if self.update else _prompt_overwrite)
        if installed_prompts:
            verb = "Updated" if self.update else "Installed"
            log.success(f"{verb} {len(installed_prompts)} agent prompt(s) under .archon-horizon/prompts/.")

        from archon_horizon.config.mcp import install_mcp_for_harnesses, write_mcp_config

        servers = write_mcp_config(self.root / ".mcp.json")
        log.success(f"Wrote .mcp.json (MCP servers: {', '.join(servers)}).")

        # Register the same servers with any non-Claude harnesses (codex) in
        # their own workspace-local config.
        try:
            from archon_horizon.config.loader import load_config

            extra = install_mcp_for_harnesses(load_config(self.root).harnesses, self.root)
            for label, names in extra.items():
                if names:
                    log.success(f"Registered MCP servers with {label}: {', '.join(names)}.")
        except Exception as exc:  # never block init on optional cross-engine setup
            log.warn(f"Skipped cross-harness MCP registration: {exc}")

        # Compile subagent descriptors into each engine's workspace-local native
        # agents (.claude/agents, .codex/agents). Re-run on `horizon run` too.
        try:
            from archon_horizon.config.loader import load_config
            from archon_horizon.subagents.compile import install_subagents

            compiled = install_subagents(
                self.root, sub_dir, load_config(self.root).harnesses
            )
            for engine, names in compiled.items():
                if names:
                    log.success(f"Compiled {len(names)} {engine} subagent(s).")
        except Exception as exc:  # never block init on optional subagent compile
            log.warn(f"Skipped subagent compilation: {exc}")

        if self.interactive:
            self._interactive_workspace_setup()

        log.success(f"Initialized workspace at {self.root}")

        if self.as_json:
            from .shared import emit_json

            emit_json({
                "initialized": str(self.root),
                "config": str(config_path),
                "config_written": write_config,
                "skills": installed,
                "mcp_servers": servers,
                "env_created": env_created,
            })
            return

        should_launch_advisor = self.post_init_advisor
        if not should_launch_advisor and self.interactive:
            from rich.prompt import Confirm

            should_launch_advisor = Confirm.ask(
                "Launch an interactive advisor to review this workspace with you?",
                default=True,
            )
        if should_launch_advisor:
            _launch_post_init_advisor(self.root)


def init(
    ctx: typer.Context,
    config_json: str | None = typer.Option(None, "--config-json", help="Pass a JSON string with configuration parameters."),
    interactive: bool = typer.Option(True, help="Run interactively. Pass --no-interactive to accept defaults without prompting."),
    advisor: bool = typer.Option(
        False,
        "--advisor",
        "--audit",
        help="After init, launch an interactive workspace advisor using the Ground agent config.",
    ),
    update: bool = typer.Option(
        False,
        "--update",
        help="Refresh an existing workspace: re-install skills, recompile subagents (pruning stale ones), and "
        "rewrite MCP/gitignore to this Horizon's bundled versions, keeping your config and content. Run after "
        "upgrading Horizon.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON; implies --no-interactive and no advisor."),
) -> None:
    """Scaffold a new workspace, or refresh an existing one with `--update`."""
    InitCommand(
        ctx.obj["root"],
        config_json=config_json,
        # JSON output is non-interactive: prompts would corrupt the stream.
        interactive=interactive and not as_json,
        post_init_advisor=advisor and not as_json,
        as_json=as_json,
        update=update,
    ).run()
