# Install the Pipeline Control Plane

The control plane's commands are `horizon` and `horizon-pipeline`; both invoke
the same runtime. Its dashboard is `/pipeline`, and its API is `/api/v3`.
Select configuration, data directories, a database, and listener ports explicitly.

## Install Only What This Machine Needs

Use Python 3.11 or newer and a release wheel that includes the built dashboard.
No Node.js or frontend build is needed on the server or worker. Install in a
dedicated virtual environment, with a pinned release and a verified wheel hash.
The example wheel path below is the release artifact selected by the operator;
it is not an instruction to install an unreleased package from a moving branch.

```sh
python3 -m venv "$HOME/.local/share/archon-horizon/venv"
export TMPDIR="$HOME/.local/share/archon-horizon/tmp"
mkdir -p "$TMPDIR"
"$HOME/.local/share/archon-horizon/venv/bin/python" -m pip install '/absolute/path/archon_horizon-VERSION-py3-none-any.whl[control-plane]'
```

Use `[pipeline-worker]` on worker machines instead. Workers need HTTPX, Tenacity,
and Pydantic; they do not import SQLAlchemy, PostgreSQL drivers, FastAPI, or the
scientific search libraries. Add `[search]` only where Lean declaration indexing
is enabled. Provider binaries and Lean toolchains belong in the selected worker
environment or pinned sandbox image, not the control-plane environment.

The baseline needs one PostgreSQL server, one API process with its scheduler and
connector loops, and one worker daemon per enrolled host. It does not need Redis,
RabbitMQ, Celery, or a frontend server. Existing Forge and Zulip services are reused.

New runs use objective-led Work and Maintenance queues; see the
[orchestration contract](design/objective-orchestration.md). The following idle-planner
safeguard applies only to explicit legacy runs with separate planner automations.
`automation_idle_recheck_seconds` defaults to 300. When such a run has usable
capacity and no queued assignment able to use it, the scheduler can release one
enabled recurring planner's condition and attach a blocker-triage obligation,
even while other sessions run. It reuses the pending planner and respects explicit
start times. The recurring rule stays intact; ordinary task dependencies and
review gates are never bypassed. Another automatic reconsideration requires
substantive work to have been created or completed. Admission budgets, expiry,
recovery delays and host health still apply. Set this option to 0 to disable the safeguard; paused runs and disabled
automations are always left alone. Use `defer_automation` to update a recurring
rule and its pending occurrence together; editing an assignment changes only that
occurrence. `no_progress: false` explicitly resets the recurrence backoff.

Forge PR creation includes the phase label and the matching policy's
`attention_labels` in the retried delivery. A lost label response reconciles the
same PR. Labels aid triage; maintainer admission should not require a label that
only that blocked maintainer can produce, and labels never grant merge approval.

An agent can `checkpoint_assignment` at a provider-turn boundary with a resume
condition or `not_before`. It releases the slot and resumes the same assignment
and provider context later, retaining open obligations. Completing a mission by
passing it unchanged to one unfinished same-role session is rejected by the
completion check. Narrow delegation and genuine decomposition remain available.
An operator can `resume_assignment` for a completed/failed context after settling
any active recurring successor; this preserves the original provider thread.

An installation operator can change an active or paused run's productive
assignment ceiling without restarting it. Submit the revision-checked command
to `POST /api/v3/commands`:

```json
{
  "operation": "set_run_budget",
  "target_id": "RUN_UUID",
  "expected_revision": 7,
  "args": {
    "max_assignments": null,
    "note": "Continue this project without an assignment-count ceiling"
  }
}
```

Use the current run revision and actual UUID. `null` removes the assignment
ceiling; a positive integer sets a finite ceiling. Existing provider contexts,
usage, pause state and token/time budgets are preserved. Agents cannot change
operator budgets. Supervisor episodes are excluded from the productive assignment
count, but still obey host capacity and explicit time/token limits.

PR creation defaults to the repository's default branch. Nondefault bases need
`stack_reason`; maintainers can retarget or close superseded proposals through
`POST /api/v3/forge/edit`. `POST /api/v3/forge/change` with `forge_item_id` amends
an open same-repository PR branch at the inspected base commit. Both operations
use the durable outbox and invalidate earlier merge approval. Amendments verify
the complete changed tree and file hashes; concurrent branch movement is surfaced
as a conflict requiring inspection. Review the new head/base before merging.

## Prepare an Explicit Installation

Use a dedicated PostgreSQL database and role. Supply the URL through an environment
variable or protected configuration, not a command argument containing a password.
`HORIZON_PIPELINE_DATABASE_URL` uses the `postgresql+psycopg` scheme. For a remote
database, configure its supported TLS verification parameters. The optional
[PostgreSQL Compose example](../deploy/pipeline/postgres.compose.yaml) binds only
loopback and requires an operator-selected image digest, storage directory, and
private password file. It can be used with rootless Podman Compose; starting it is
an explicit infrastructure action. Existing PostgreSQL is equally supported.

Horizon requires explicit absolute storage paths; it does not automatically choose
`~/.horizon` or `~/.horizon-pipeline`. Configure these locations for each host:

| Configuration | Contents |
| --- | --- |
| Server `state_root` | Artifacts and disk-backed server scratch under `artifacts/` and `tmp/` |
| Worker `workspace_roots` | Managed project checkouts and session worktrees |
| Worker `journal_root` and `token_file` | Durable execution/publication journals and the private worker credential |
| Harness `provider_home` and `scratch_root` | Provider authentication/context and per-execution temporary files |
| Worker `lean_build.root` | Shared Lean build checkouts and managed compiler caches |

PostgreSQL and Podman image storage are configured separately. Keep workspaces
separate from worker journals, credentials and provider homes. JSON paths must use
their absolute spelling; `~` is not expanded.

```sh
horizon-pipeline --config /absolute/path/pipeline/server.json init \
  --state-root /absolute/path/pipeline/state \
  --public-url https://horizon.example.org
```

The first invocation prints a redacted plan and performs no writes or database
connections. Add `--apply` after reviewing it. Apply creates private files and
directories and refuses to overwrite an existing configuration. In a terminal,
omitted state-directory and public-URL fields are prompted for; an omitted database
URL is read from a named environment variable without displaying its value. Use
`--interactive` to request those prompts explicitly or `--non-interactive` for
automation. Missing required fields then return structured JSON errors. Both paths
use the same typed configuration validation. This does not provision containers
or enroll a host automatically.
Subsequent configuration changes are deliberate operator edits to this versioned,
closed JSON schema; unknown fields are rejected.

```sh
horizon-pipeline --config /absolute/path/pipeline/server.json migrate
horizon-pipeline --config /absolute/path/pipeline/server.json create-admin operator
horizon-pipeline --config /absolute/path/pipeline/server.json serve
```

Migrations run only when requested; service startup checks the installed revision.
The password prompt does not echo the password or place it in process arguments.
Use `--password-stdin` with a protected provisioning input for unattended setup.
An HTTPS reverse proxy must forward to the configured loopback listener. Only
explicit `trusted_proxy_hosts` are trusted. For an isolated local development
installation, an `http://127.0.0.1:PORT` public URL enables non-secure cookies;
remote HTTP and remote non-secure cookies are rejected.

Open `https://horizon.example.org/pipeline` and sign in. Browser authentication
uses an HTTP-only session cookie; worker credentials never enter browser storage.
Create project, mission, repository, integration, host, harness, and workspace
records together using the versioned [project recipe](../deploy/pipeline/project.example.json):

```sh
horizon-pipeline --config /absolute/path/pipeline/server.json bootstrap-project \
  --operator operator --input /absolute/path/project.json
horizon-pipeline --config /absolute/path/pipeline/server.json bootstrap-project \
  --operator operator --input /absolute/path/project.json --apply
```

Choose a new recipe UUID and replace all example paths, source commits, objectives,
provider versions, model names, and sandbox pins. Named `{"$ref": "key"}` values
refer to earlier records; literal existing UUIDs are also accepted. `operator` is
a reserved reference to the applying administrator. Preview validates the complete
expanded recipe without contacting the database or integrations. Apply uses the
same domain services as the API in one transaction and returns the created IDs.
Repeating an identical recipe ID returns its receipt. Reusing that ID with changed
contents is rejected; a failed transaction creates no partial installation.

Optional review presets install editable descriptors and repository/phase policies:
careful roadmap review during preprocessing, proportionate roadmap review during
formalization, and strict library review during postprocessing. They do not add a
workspace gate or require a fixed reviewer roster. The maintainer selects relevant
reviewers, order, redispatch, or a justified direct merge. Custom descriptors and
policies can instead appear directly in the recipe's records.

The example Forge integration is disabled until its explicit credential reference
is configured. Recipes contain secret references, never provider/Forge passwords.
Create the remote repositories through their normal operator workflow and prepare
the declared Git workspaces on the worker host before setting them ready. The
bootstrap command creates database records; it does not clone repositories, mark
unverified paths ready, install providers, or make external API calls. Each slot
needs a distinct writable checkout/branch. For an existing local checkout, verify
its configured source commit and branch. After host credential enrollment, run
the read-only verifier locally on that worker host:

```sh
horizon-pipeline --config /absolute/path/pipeline/server.json verify-workspaces \
  --operator operator --worker-config /absolute/path/pipeline/worker.json
horizon-pipeline --config /absolute/path/pipeline/server.json verify-workspaces \
  --operator operator --worker-config /absolute/path/pipeline/worker.json --apply
```

This operator command needs control-plane dependencies and explicit database access
on the provisioning machine; ordinary worker runtime still needs only the worker
extra. It authenticates the enrolled host token, checks allowed local roots and
symlink boundaries, verifies the branch and pinned base ancestry, and refuses
uncommitted/untracked initial content. It performs no Git writes, cloning, resets,
or cleanup. Apply revision-checks the verified initial workspace records before
marking them ready. Existing ready/active workspaces are excluded. This step is
required before workers can claim newly registered checkouts.

Workers advertising `workspace_preparation: 1` can create additional worktrees
when all existing checkouts are occupied or retained by unfinished sessions.
The scheduler allocates an assignment-specific path under the host workspace
root and pins the verified source commit; the worker prepares it, checks its
identity, and delivers a durable receipt before launching the provider. Git
objects are shared, but mutable files and branches are separate. Ignored Lean
caches are not copied. Checkpointed contexts keep their worktrees; completed
checkouts return to the reusable pool. Initial source checkouts still require
the verification above. Existing shared contexts need explicit verified
relocation; an upgrade never silently moves their provider state.
Hosts with container harnesses currently use explicitly provisioned independent
checkouts and do not advertise linked-worktree preparation: their Git metadata
must be reachable through the configured container mounts.

The same records remain available through `/api/v3/records/{kind}`. The
authenticated `/api/v3/schema` endpoint describes accepted creation models and
commands. Settings currently exports redacted configuration; it is not a complete
graphical provisioning wizard.

### Changes Across Repositories

Agents can read a project's private Forge files at an exact commit and inspect
PR files, diffs, comments and reviews through the scoped read broker. PR inspection
checks the head before and after the remote read; a changed head requires a fresh
request. Reads use the configured integration credentials inside the control plane.
For Forgejo tokens, repository access alone does not authorize issue/PR comment
reads: the disposable Forgejo 16.0.5 protocol test required the separate issue
scope. A contributor broker token uses `write:repository` and `write:issue` for
its branch/file/PR/comment operations; add `read:user` when account inspection is
enabled. A read-only integration can use the corresponding read scopes. Token
scopes do not grant repository membership or bypass branch protections.

The daemon publishes its primary workspace's Git checkpoints using the explicitly
configured `publication_remotes`. Publication runs in dedicated background lanes
(`publication_concurrency`, default 1, maximum 4), independently of occupied agent
slots. `publication_poll_seconds` defaults to 5; `checkpoint_seconds` defaults to
300 and controls periodic recovery snapshots while a provider runs. Snapshots use
a private index and retain the agent's branch/index. They capture files observed
during the scan, not an atomic or build-validated state; a final checkpoint follows
provider shutdown. Ignored files and external repositories are not workspace backups.

`max_request_seconds` bounds one provider turn. `max_execution_seconds` bounds one
physical execution episode, including setup, API reconciliation, provider turns,
and continuations within its lease; it defaults to 14,400 seconds (four hours).
Transport and journal recovery can create another lease with its own episode
budget. When an episode reaches its budget, the worker checkpoints its workspace
and emits a `yielded` stop receipt with reason `execution_budget_reached`; the
scheduler completes an accounted deliverable or records unfinished work for
explicit replanning or recovery. Choose a shorter value for preprocessing profiles
(for example 1,800 seconds) and keep the four-hour default for a formalization
episode expected to require several turns.

Transient Git/network failures retry indefinitely with capped exponential backoff
and jitter. Authentication/configuration errors and conflicting recovery refs are
blocked for repair, without discarding their local commits. Immutable publication
receipts also survive prolonged API outages. Stop receipts wait for checkpoint
discovery acknowledgements so completion cannot overlook known local publications.
The daemon retains unpublished data and pauses only when its explicit byte floor
or emergency disk-usage threshold is reached; a prolonged outage still requires
sufficient worker disk space. A worker disk lost before its
first verified remote push remains a data-loss risk.
Snapshot failures retain a durable recovery job with exponential backoff. They do
not stop unrelated worker lanes. The workspace remains quarantined until its
files and physical process stop are reconciled; a daemon restart resumes this job.
Local operation timeouts are recoverable failures, not invalid configuration.
Acknowledged journal records and recovery refs currently remain retained as well;
automatic deduplication-aware reclamation is not implemented. Monitor the configured
journal/disk budget even when publication is healthy. Do not blanket-delete the
journal or recovery refs to reclaim space: they carry reconciliation evidence.

Inspect the selected worker's queue and checkpoint health without stopping it:

```sh
horizon-pipeline worker-publications --worker-config /absolute/path/pipeline/worker.json
```

After repairing credentials or the reported Git conflict, retry the original
blocked operation without changing its idempotency key. Restart the selected
worker daemon after editing its configuration or credential files so it reloads
them; existing journal records survive that restart:

```sh
horizon-pipeline worker-publications --worker-config /absolute/path/pipeline/worker.json \
  --retry OPERATION_UUID --note "Corrected publication credentials"
```

The dashboard Changes view shows known unpublished checkpoints, repair counts,
oldest pending publication, and last confirmed push. The confirmation time is when
the control plane first records the worker's verified remote receipt. These are control-plane
observations: during an API outage the local journal is the authoritative source
for checkpoints whose receipts have not arrived. Worker-managed retries must be
requested on that worker; a server record edit cannot restart its local Git job.

For changes to the roadmap or destination library,
agents upload file blobs and queue `/api/v3/forge/change`. The broker creates
`horizon/changes/<operation UUID>` from a full base commit and applies one atomic
Forgejo file batch. Updates/deletions require the previous file blob SHA. Each
batch allows at most 200 files, 1 MiB per file, and 8 MiB total; upload requests
remain subject to the server's configured request limit. Larger extractions can
be split into reviewable PRs.

After the operation settles, its result points to the created commit artifact;
the existing Forge PR command uses the returned branch. Retries reconcile that
branch and commit rather than force-overwriting it. Protected/default branches
remain under the maintainer's normal merge policy. This uses native Forgejo
branch and contents APIs, and does not install another Git service or give an
agent the broker's credentials. The selected Forge account must be allowed to
create proposal branches and PRs; grant final review/merge through the separately
configured maintainer identity when the destination requires it.

## Dashboard Access

The API listens on `127.0.0.1:8788` by default; the dashboard is at `/pipeline`.
For the default local installation, open `http://127.0.0.1:8788/pipeline` on the
control-plane host. The loopback listener is not reachable directly from another
machine. Local HTTP configuration uses that origin as `public_url` and
`secure_cookies: false`.

For remote browser access, use an HTTPS reverse proxy and set `public_url` to the
exact browser-facing origin, without `/pipeline`. Keep `secure_cookies: true` and
restart the control-plane service after updating its configuration. Browser writes
must originate from `public_url`; merely forwarding a port does not update that
setting. Keep the API on loopback when the proxy runs on the same host.

For private access with [Tailscale Serve](https://tailscale.com/kb/1312/serve):

1. Connect the control-plane host and the viewing devices to Tailscale, and enable
   HTTPS certificates for the tailnet as described in Tailscale’s documentation.
2. On the control-plane host, proxy the local API with:

   ```sh
   tailscale serve --bg http://127.0.0.1:8788
   ```

3. Use the HTTPS origin printed by Serve as Horizon’s `public_url`, enable secure
   cookies and restart Horizon. Open that address with `/pipeline` appended.
4. Restrict Tailscale access to the service’s HTTPS port, normally TCP 443. Sign in
   with a Horizon account whose project permissions match the intended access;
   use a viewer membership for collaborators who should only monitor work.

[Tailscale device sharing](https://tailscale.com/kb/1084/sharing) lets people outside
your tailnet reach the shared host using their own Tailscale accounts and clients.
They also need Horizon accounts, but do not need OS accounts or SSH access on the
control-plane host. Restrict shared-device access to the dashboard’s HTTPS port;
sharing a device must not inadvertently expose other listening services.

Forgejo and Zulip embedded views have their own HTTPS and authentication
requirements; see
[browser integrations](pipeline-browser-integrations.md).

## Enroll a Worker

Workers send independent health heartbeats even when storage prevents admission.
The Resources view distinguishes storage pressure from a missing heartbeat; queue
details distinguish logical conditions from unavailable hosts, retained-context
affinity, unreconciled requests and occupied workspaces. Configure the explicit
byte floors and emergency usage threshold for each installation. The 20% free-space
setting is a cleanup target reported in health, so falling below it does not by
itself pause work; cleanup should reclaim owned caches and diagnostics while
preserving journals, workspaces, artifacts and recovery evidence.

For independent supervision install the worker watchdog service/timer from
`deploy/pipeline`, adjusting executable and configuration paths as for the worker:

```sh
horizon-pipeline worker-watchdog --worker-config /absolute/path/worker.json \
  --restart-service archon-horizon-pipeline-worker.service
systemctl --user enable --now archon-horizon-pipeline-worker-watchdog.timer
```

The watchdog checks per-component progress deadlines and process identity. Three
failed checks request `try-restart`, with a five-minute cooldown. Responsive
workers reporting storage pressure are not restarted. A deliberately stopped
service stays stopped. Progress and restart counters live beside the journal;
no provider prompt or additional agent is required for this recovery.

Create the host and its enabled harness bindings in the control plane, then issue
the host-scoped credential into a new private file:

```sh
horizon-pipeline --config /absolute/path/pipeline/server.json host-key HOST_UUID \
  --output /absolute/path/private/worker-token
```

Transfer that file securely to the worker. The destination is created exclusively
with mode `0600`; the token is never printed. Do not reuse an administrator token
as a worker token. Configure the worker using
[worker.example.json](../deploy/pipeline/worker.example.json). Replace the example
UUIDs and paths with the enrolled records. Configuration validation rejects
duplicate harness IDs, remote HTTP, relative paths, and workspaces overlapping
provider state or the recovery journal. The token file must be owned by the daemon
user, private, and a regular file rather than a symbolic link.

```sh
horizon-pipeline worker --worker-config /absolute/path/pipeline/worker.json
```

`slots` is this daemon's local execution concurrency. Global admission also checks
the database's host/harness bindings and shared provider-account limits. An objective run
can use one effective slot; explicit legacy runs require at least two. Model and
reasoning allowlists are optional and explicit; without them the worker requires
the server's pinned settings to match its local harness configuration.

After the worker has connected and its checkouts are ready, launch a run from the
[run recipe](../deploy/pipeline/run.example.json), using IDs returned by bootstrap:

```sh
horizon-pipeline --config /absolute/path/pipeline/server.json launch-run \
  --operator operator --input /absolute/path/run.json
horizon-pipeline --config /absolute/path/pipeline/server.json launch-run \
  --operator operator --input /absolute/path/run.json --apply
```

Launch IDs are durable idempotency keys. The recipe selects objective mode by
default; an existing deployment's legacy workflow must specify
`"orchestration": "legacy"`. The simpler objective-only command defaults to enabled
hosts and the phases supported by the project repositories:

```sh
horizon-pipeline --config /absolute/path/pipeline/server.json launch-objective OBJECTIVE_UUID \
  --operator operator
horizon-pipeline --config /absolute/path/pipeline/server.json launch-objective OBJECTIVE_UUID \
  --operator operator --apply
```

Use repeated `--phase` options for a requested subset beginning with preprocessing,
`--human-approval` to pause at phase boundaries, and `--queue-policies` for a JSON
mapping of category slots/bounds/budgets/provider defaults. A run recipe can start
at formalization with a graph objective directly, or at postprocessing using
exact workspace source inputs. Explicit compatibility workflows retain typed
baseline inputs. An
already-active objective is resumed instead of launching another owner.

Apply migrations through `0030_optional_subagent_limits` explicitly before starting this
code. Revision 0028 introduced objective queues; 0029 adds default graph planning
and recorded phase transitions without a compulsory milestone snapshot.
Revision 0030 allows uncapped native delegation for new harness bindings; existing
subagent limits, provider quotas and execution reservations are preserved.
Existing run and policy rows keep their legacy/default-required behavior; no live
run or pinned instruction bundle is converted. Review a new objective launch
separately before changing existing automation ownership. No destructive downgrade
is provided; recovery uses a verified backup.

For explicit `workflow: milestones` compatibility preprocessing, clone the roadmap knowledge repository into its
registered host path and check out the registered branch and pinned head, then
run `verify-workspaces --apply`. Automatic assignment worktree provisioning does
not prepare persistent knowledge-repository checkouts. Verification workers need
that checkout to be `ready`; a queued dependency cannot create it by itself.

Bind a project Zulip channel and register the exact topic `Horizon operations`
for material operational incidents. An absent or ambiguous binding is reported
as unconfigured; agents must not guess another project's channel. Ordinary
workers and maintainers can use `agent context --view operations` and the native
`orchestration-auditor` helper on demand. The helper reports to its parent and
does not perform mathematical review, builds or environment cleanup.

Use a pinned rootless sandbox image and verify its provider binary, toolchain,
network access, and write restrictions before enrolling production work. The
example has an intentionally invalid image digest placeholder and cannot be run
unchanged. Unrestricted execution is an explicit alternate configuration, not a
fallback after sandbox failure. Provider authentication is prepared before the
daemon starts; unattended jobs must not depend on interactive login or upgrades.
The current Codex adapter accepts `deny` with `read_only`/`workspace_write`, or
`preauthorized` with an externally isolated rootless sandbox. The Claude adapter
accepts read-only tools or externally isolated execution with the supported tool
allowlist. Unsupported settings fail closed. Native subagents have no
Horizon-imposed cap by default: omit `max_parallel_subagents` from the host/harness
binding, or set it to `null`. Provider-native limits and tool permissions still
apply. Zero explicitly disables native delegation; a positive value opts into a
Codex cap and reserves bounded child capacity alongside the parent. Planners and
one-slot hosts retain native delegation when the setting is uncapped. Claude’s
adapter supports uncapped delegation or disabling it, but rejects a positive cap
because it cannot reliably enforce one. Explicit shared provider quotas still
govern admission, and uncapped children are accounted for as they are observed.
New objective launches without a provider quota create an outage guard with no
normal concurrency cap. After a provider failure, it allows one recovery probe
at a time; repeated failures still open the circuit.
Existing bindings keep their settings after migration; set their value to `null`
explicitly to opt into uncapped delegation. Existing provider quotas are separate:
set their `max_concurrent` to `null` to retain outage protection without a quota.
Build pools always require a positive concurrency bound. Disabled auto-compaction and
unsupported approval/tool settings are rejected rather than silently ignored.

Native capabilities are enabled in the normal container profile. Codex uses the
newer multi-agent mode and live web search; Claude loads its native settings,
plugins, hooks and MCP configuration, with the provider's current default tools.
Configure these in the dedicated provider home or workspace. For an explicit
override, harness `settings` accepts `codex_multi_agent_v2: false`,
`codex_web_search: "cached"` or `"disabled"`, and
`claude_native_configuration: false`. Read-only Claude runs retain restricted
settings and MCP discovery. An explicit Claude tool list selects native tools
without Horizon maintaining a separate catalog for writable runs.
Claude's idle background-helper ceiling is disabled inside the supervised run;
Horizon still enforces the execution deadline and lease.
See [native provider capabilities](agent-context.md#native-provider-capabilities)
for the audited defaults and remaining execution bounds.

Each local harness may specify an `environment` object for tool visibility. Only
`PATH`, `ELAN_HOME`, `LAKE_HOME`, `LEAN_PATH`, `LEAN_SRC_PATH`, `XDG_CACHE_HOME`,
`LANG`, `LC_ALL`, and `TZ` are accepted. Paths must be literal absolute paths;
colon-separated search paths cannot contain empty or relative entries. Values do
not expand `$HOME`, `$PATH`, or `~`. Omitting this object preserves the minimal
system PATH. The daemon does not inherit the operator shell's environment or
credentials, and these settings cannot override `HOME`, provider configuration
homes, scratch paths, or Horizon credentials.

For host execution, include the selected Horizon venv's `bin`, provider `bin`,
and Elan `bin` directories in `PATH`, followed by the system tool directories.
Set `ELAN_HOME` to the intended installed toolchains if they are outside the
dedicated provider home. For a container, use paths **inside the image**, as in
the worker example, and install Horizon's worker CLI, the provider, Git, and the
required Lean toolchains there. Environment settings do not install tools or
mount host directories. A read-only Elan installation must already contain the
project's pinned toolchain; use configured writable storage when upgrades are
needed. Verify that `horizon-pipeline`, `lake`, and `lean` are available in the
actual agent environment before launching the run.
Host provider executables are resolved using this explicit PATH, version-checked,
and launched by that resolved path. With Podman, the launcher and its helpers use
the daemon's host PATH and home; the configured tool environment is passed into
the container separately. An image PATH need not contain host Podman tools.

Use a dedicated `provider_home`, with authentication prepared under `.codex` or
`.claude` as appropriate. Copy required authentication deliberately rather than
copying an old provider home wholesale: old configs may enable legacy hooks or
load unrelated skills. Keep the original provider state in the protected backup
until all previous work has been recovered.

The daemon supplies execution-scoped API credentials and `HORIZON_AGENT_STATE`
for durable agent request identities. Uncertain requests reuse the same identity
after a CLI restart. Requests outside the supported replay window remain recorded
but require reconciliation. Host journals, unpublished Git recovery refs, active
provider state, and workspace files are protected recovery data.
For Codex running on the host with `workspace_write`, each initial or resumed
request adds only its assignment's `api-intents` directory to Codex's writable
roots, so the agent CLI can persist requests outside the checkout. This does not
grant write access to the provider home or worker journal. Container execution
continues to use its existing scoped mounts.
Within a dispatched session, `horizon-pipeline agent pending` exposes unsettled
intents and `horizon-pipeline agent replay` retries a bounded batch. A resumed
execution reuses the assignment's existing intent keys with its fresh token;
the original execution provenance is retained. Every replay still reaches the API
and rechecks current authority, even when the server already has a receipt.
Rejected intents remain completion blockers. After repairing or deliberately
delegating the issue, `horizon-pipeline agent resolve-intent KEY --note EXPLANATION`
records a decision obligation and its disposition in the central ledger before
clearing the local blocker. A lost acknowledgement preserves both repair intent
and original blocker for the next authenticated retry.

`horizon-pipeline agent reference REFERENCE_UUID --workspace /absolute/workspace`
prints authenticated BibTeX and maintains a bounded cache inside that workspace.
Add `--path` to obtain the local cache filename. Every retrieval revalidates current
authorization and reference revision; this cache is not a source of permissions.
Set the worker's `max_offline_replay_seconds` no higher than the control plane's
storage policy. Agent CLI calls receive the authoritative horizon in their
execution environment. API idempotency retention must exceed that horizon.

## Supervision and Checks

The example [API](../deploy/pipeline/archon-horizon-pipeline-api.service) and
[worker](../deploy/pipeline/archon-horizon-pipeline-worker.service) user services
use distinct names and explicit configuration paths. Review and adapt them before
installing them under the chosen user's systemd directory. They are never installed
automatically and do not replace any existing Horizon service. Each process has
bounded restart pacing and its own journal rate limit. They do not change the
machine's global journald retention policy.
The API's own logs rotate beneath `state_root/logs` within
`service_log_budget_bytes`; oversized diagnostic records are explicitly truncated.
Setting that budget to zero disables file logging. These service logs are not proof
evidence or a replacement for durable execution records.

The optional [watchdog timer](../deploy/pipeline/archon-horizon-pipeline-watchdog.timer)
runs outside the API process. Its explicitly selected user-service restart command
probes local `/health/live`, waits for three consecutive failures, and allows at
most one restart per five minutes. Database readiness failures alone do not trigger
a restart. It uses `systemctl --user try-restart`, so an operator-stopped service
stays stopped. Review and enable this separate timer only for the new installation;
it is not activated by `serve` or `doctor`.

```sh
horizon-pipeline --config /absolute/path/pipeline/server.json doctor
horizon-pipeline --config /absolute/path/pipeline/server.json export-config
```

`doctor` is read-only and emits JSON with database revision, free-space, dashboard
bundle, an external HTTP readiness probe, expired worker leases, unconfirmed stops,
publication status counts, and outbox ages. Failure or blocked recovery produces a
nonzero exit status. Add `--worker-config /absolute/path/worker.json` to check that
worker's private host credential, local directories, Git/provider executables,
private provider-auth file presence, and availability of its pinned Podman image.
It never invokes a model, logs in, pulls/builds an image, or writes to the Forge.
Auth-file presence is not an online authentication check; external/keychain-based
authentication may need independent verification. A database or API failure must be repaired outside the
agent dispatch loop; restarting an agent does not repair the infrastructure.

## Cleanup and Backups

```sh
horizon-pipeline --config /absolute/path/pipeline/server.json cleanup
horizon-pipeline --config /absolute/path/pipeline/server.json cleanup \
  --apply-preview /absolute/path/reviewed-cleanup.json
horizon-pipeline --config /absolute/path/pipeline/server.json cleanup --database
horizon-pipeline --config /absolute/path/pipeline/server.json backup \
  --destination /absolute/path/backups/new-backup-directory
horizon-pipeline --config /absolute/path/pipeline/server.json verify-backup \
  --backup /absolute/path/backups/new-backup-directory
```

Filesystem cleanup removes owned diagnostic directories with terminal, unpinned,
resolved retention markers. Successful diagnostics use the successful-work TTL;
resolved failures use the longer failure TTL. Eligible diagnostics also obey their
byte budget. Apply recomputes eligibility and compares the exact marker with the
reviewed preview. Backup rotation applies only under `state_root/backups`: it
enforces the count/byte policy while retaining the newest checksum-verified set.
Unverified backups and identifiable partial backups require operator investigation
and are not deleted automatically. A sole verified backup remains protected even
when it alone exceeds the budget.

Database cleanup is a separate bounded preview/apply operation (`--database` with
the same `--apply-preview` option). It removes settled API receipts only after both
their expiry and idempotency-retention horizon, protects receipts of live agent
executions and durable setup/run-launch receipts, and removes old unreferenced dashboard invalidations. Pending or handled
notification references, semantic status events, and proof evidence are preserved.
Each apply rechecks the selected batch under the domain transaction lock.
The API also applies these database rules automatically, at most 500 records
every 300 seconds. Filesystem and backup cleanup remains explicit because backup
eligibility includes reading and hashing the complete backup set.

Server requests receive an `X-Request-ID` and a bounded OpenTelemetry span.
Owned service logs contain that request id, trace id, route template, response
status and duration. Bodies, credentials and raw query strings are excluded.
No collector is required; exporting spans is not enabled by default.

Workers separately reserve diagnostic bytes before launching a request. Their
`journal_diagnostic_max_bytes`, `journal_diagnostic_retention_seconds`, and
`journal_failure_diagnostic_retention_seconds` bound capture and retention. Cleanup
requires a terminal request and execution, a cleared recovery state, an acknowledged
execution outcome, and acknowledgement of all associated API/Git operations.
Admission pauses when protected diagnostics fill the budget. Artifacts, journal
identities, native provider state, Git recovery refs, workspaces, and unresolved
failures are retained. Native provider directories may contain shared session data;
they have no automatic file-deletion policy until a provider-specific cleanup
contract can establish safe resume pins. Protected data can grow; disk pressure
pauses admission rather than deleting evidence.

Backup requires PostgreSQL client tools compatible with the server. It exports a
PostgreSQL snapshot, runs `pg_dump` against that snapshot, copies matching shared
artifact blobs, and verifies identities. The new backup directory contains a
database dump, content-addressed blobs, a checksum manifest, and redacted
configuration. Operator secrets, worker-local journals, provider state, and
unpublished workspace files are explicitly excluded and need their own protected
backup plan. Do not discard the most recent verified recovery set. An incomplete
backup remains under an identifiable `.horizon-backup-*` staging directory and is
never advertised as complete.

A checksum verification is not a restore test. Restore into a new, empty database
and new artifact directory before switching a service. With explicit `PGHOST`,
`PGPORT`, `PGDATABASE`, `PGUSER`, and protected password handling aimed at that new
database, use PostgreSQL's native restore:

```sh
pg_restore --exit-on-error --single-transaction --no-owner --no-acl \
  --dbname "$PGDATABASE" /absolute/path/backup/database.dump
```

For each manifest blob, restore `blobs/SHA256` to
`NEW_STATE_ROOT/artifacts/SHA256[0:2]/SHA256[2:]`, preserving its verified content.
Restore the independently protected operator secrets and configure the new state
root/database explicitly. Run `doctor` and an isolated proof-evidence read before
switching traffic. The operations integration test exercises a real dump/restore
into a disposable database and verifies matching artifact content. Production
restore rehearsals, backup scheduling, off-host copies, and retention remain
operator responsibilities in this initial implementation.

## Recover an Uncertain Host Claim

A lease timeout cannot prove that a process stopped. Until the worker reports its
stop, the API retains its workspace, provider capacity and host slot. For a machine
that cannot return, physically fence its processes first. An installation
administrator may then submit `confirm_host_stopped` for the lost/cancelled
execution, with its current `expected_revision` and arguments
`{"machine_fenced": true, "note": "...", "evidence": "..."}`. The evidence should
identify the actual process/container/hypervisor fencing result. Agents cannot
issue this acknowledgement and elapsed time is not a substitute for it.

After stopping the local worker daemon, resolve an expired uncertain claim with:

```sh
horizon-pipeline worker-reconcile-claim \
  --worker-config /absolute/path/pipeline/worker.json \
  --note 'Host fencing confirmed centrally; previous claim reconciled'
```

The command takes the daemon lock and authenticates a read of the host's
unconfirmed executions. It only resolves the local claim when that list is empty
and complete, recording the note durably. It never edits away execution records
or assumes that failed HTTP requests were not committed. The normal worker then
resumes admission. A configured `provider_version` is the numeric version token
from the binary's `--version` output, not the complete banner.

## Managed Lean Checks

The worker example enables `lean_build`, independently of agent `slots`. Start
with one expensive build per host. Increase `max_parallel_builds` only after
measuring peak RAM, leaving room for active language servers and the worker.
Use container CPU/memory limits as the enforcement boundary; the build-slot
budget coordinates cooperating helper calls, not arbitrary shell commands.
Trusted Lean/library audits use that same compiler budget. Set `lean_checks: true`
to enable the generic library lane, independently of agent `slots`. It requires
an explicitly unrestricted managed-build host profile. Queue a ready library
workspace with `POST /api/v3/lean/verifications` and `workspace_id`,
`source_commit_oid`, `base_commit_oid`; follow its status and `check_id` using
`GET /api/v3/lean/verifications/{id}` and `GET /api/v3/lean/checks/{id}`.
`milestone_checks` and old milestone endpoints remain compatibility options for
existing strict projects. New graph planning requires neither flag nor a
milestone manifest. Checks audit exact-source elaboration and admissions; agents
and maintainers assess statement meaning and integration quality.
`max_parallel_preparations` separately bounds host-wide dependency downloads and
checkout copies (default 1). Per-repository locks still coalesce identical Git
fetches, and cache restoration during a build shares its compiler lane. These
limits apply to managed helpers; native shell commands remain subject to the
configured process/container limits.

`lean_build.root` is explicit build storage outside journal,
credentials, workspaces, provider homes, and scratch. Every container harness
must declare this same host directory as a read-write `/horizon-build` mount,
including in its enrolled sandbox policy. The loader creates the directory;
it does not install packages or modify provider configuration. Install Horizon
and the project's pinned Lean toolchain in the worker image and put their tools
on its configured PATH. Host Codex workspace-write sessions receive the build
directory as an additional writable root, alongside the intent journal.

The rebuildable Lake artifact stores within that root have a host-wide
`cache_max_bytes` budget (10 GiB) and `cache_max_age_seconds` expiry (7 days).
Managed builds share cache leases; eviction requires exclusive access and never
removes the Git object pools borrowed by dependency checkouts. The entire build
root is therefore **not disposable** while those checkouts remain.

Before managed builds, checkout locks also protect pruning of old, untracked
native C/object outputs under exactly `.lake/build/ir`. The per-checkout
`native_cache_max_bytes` target defaults to 4 GiB; outputs younger than
`native_cache_min_age_seconds` (7 days) remain even above that target. This is a
retention target, not a hard quota: active/recent output is protected, and direct
shell builds do not participate in managed locks. Lean `.olean`/`.ilean` files,
source files, Git data, scratch contents, and provider recovery state are not
automatically deleted. These limits do not bound total workspace disk usage.
Disk reserve checks stop managed compilers before free capacity is exhausted;
container memory limits enforce RAM bounds independently of cache retention.
Successful cache scans are reused for five minutes across repeated checks;
free space below reserve or changed retention settings bypass that cadence.
Incomplete scans retry on the next check. Cleanup is best effort and cannot
turn a successful build into a failure because a cache file disappeared.

Agents receive `HORIZON_LEAN_BUILD` and use:

```sh
horizon-lean-check MyProject.Module
horizon-lean-check --probe MyProject.Module
horizon-lean-check --lean MyProject/Module.lean
```

The equivalent `python -m archon_horizon.pipeline.worker.lean_build` invocation
works without the console script. The helper shares host slots and checkout
locks, uses existing incremental Lake builds, bounds queue/build waiting, and
stops owned compiler processes on timeout/cancellation. Exit 75 is deferred,
124 is timed out, and other nonzero results are failures. It works independently
of both Horizon APIs; an API outage cannot prevent local verification. It does
not automatically retry compiler errors or create repair assignments.

JSON output includes the toolchain, source fingerprint where supported,
diagnostics and timings. A successful check whose inputs changed during the
check becomes deferred. `snapshot_verified: false` means a stable fingerprint
could not be established (for example an unsupported dependency layout); the
result is not exact-revision publication evidence. A build alone does not prove
absence of admissions or mathematical faithfulness.

`artifact_cache` defaults to true. Every worker on one physical host should use
the same `lean_build.root`, so Lake artifacts and the host's mathlib build
outputs are reused across workers and harnesses. Different hosts keep separate
roots. Dependency source checkouts remain per-workspace for isolation, while
their Git object pool and rebuildable Lake artifacts are shared. Admission slots
remain host-wide; there is no custom remote artifact service in the pipeline.

If bounded cleanup cannot restore the free-space reserve, new checks are deferred.
This is an admission check, not a disk quota. Configure a filesystem quota where
necessary; cleanup preserves active builds and the protected data described above. Existing
provider Lean LSP integrations remain configurable through their provider homes;
this change does not install an MCP server or add another scheduler dependency.

## Frontend Development Checks

The release wheel already contains the dashboard. Developers rebuilding it need
Node.js 20 or newer and the lockfile dependencies. Playwright is pinned as a
development dependency; install its matching Chromium once with
`npx playwright install --with-deps chromium`, then run `npm run typecheck`,
`npm test`, `npm run build`, and `npm run test:pipeline` from
`src/archon_horizon/frontend`. The fixture server binds an unused loopback port
and never contacts an installed Horizon service. Set `HORIZON_BROWSER_ARTIFACTS`
to an explicit writable screenshot directory in CI; the development default is
`~/.horizon/development-tmp/pipeline-redesign/browser`. The separate actual-API
browser test requires an explicit isolated demo-access file and is not run by the
fixture test command.

Exercise restores against a disposable database and state directory. A
schema-changing rollback requires the compatible backup, not just older binaries.
