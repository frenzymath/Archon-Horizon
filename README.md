# Archon Horizon

![Version](https://img.shields.io/badge/version-0.2.0-blue)

Archon Horizon coordinates automatic Lean formalization across projects and worker
machines. Missions form a tree of scoped outcomes with explicit acceptance
criteria. One PostgreSQL control plane schedules their assignments, preserves
provider context, records progress, and reliably publishes work to Forge. The dashboard
brings together roadmaps, graphs, missions, Activity, Forgejo, and Zulip.

## Installation

Use Python 3.11 or newer. Release wheels include the dashboard. To install
from a source checkout, also use Node.js 20 and npm:

```sh
git clone https://github.com/frenzymath/Archon-Horizon.git
cd Archon-Horizon
python3 -m venv .venv
. .venv/bin/activate
npm --prefix src/archon_horizon/frontend ci
npm --prefix src/archon_horizon/frontend run build
python -m pip install '.[control-plane]'
```

Workers install `[pipeline-worker]`. Add `[search]` where Lean indexing is used;
use `[dev]` for development. Configure PostgreSQL and explicit installation paths
as described in the [setup guide](docs/pipeline-setup.md):

```sh
horizon --config /absolute/path/server.json init --interactive
# Review the plan, then repeat with --apply.
horizon --config /absolute/path/server.json migrate
horizon --config /absolute/path/server.json create-admin operator
horizon --config /absolute/path/server.json serve
```

`horizon`, `horizon-pipeline`, and `python -m archon_horizon` invoke the same
implementation. The dashboard is at `/pipeline`; the authenticated API is
`/api/v3`. Configuration and database changes are explicit commands, never side
effects of importing the package or opening the dashboard.

## Runs And Review

A run selects pre-processing, main formalization, or post-processing. All use
the same queue, worker/maintainer roles, continuation, and publication machinery.
Each phase can run independently. The workspace is free working space; review
policies apply to changes entering roadmap or library repositories.

New default runs start with one root maintainer that chooses work, reviews
results and owns phase completion. Workers deliver scoped results; maintainers
request concrete repairs or accept them. Planning belongs within these roles.
Event waits release execution capacity instead of keeping a model polling.
Agents can inspect dashboard-equivalent run observations with
`horizon-pipeline agent context --view operations` and invoke an
`orchestration-auditor` native helper for a concrete anomaly. A standing
orchestrator is not part of this default path. See the
[phase workflows](docs/architecture.md#phase-workflows) and
[recovery contract](docs/coordination-recovery.md).

Assignments retain their obligation ledger and provider context across
continuations. Host journals preserve uncertain requests and unpublished Git
checkpoints for retry. Reviewers use pinned descriptions and exact PR revisions;
the maintainer chooses useful perspectives and makes the final merge decision.

Delegation creates a narrower child mission with a recorded purpose and bounded
scope. The mission tree records responsibility; the mathematical dependency graph
records prerequisites. Agents revise their own mission subtree through
revision-checked API operations and inspect shared capacity before adding work.
Queue readiness is derived from mission state, assignment conditions, leases and
available resources. An execution ending is distinct from its mission being accepted.
See [mission coordination](docs/mission-coordination.md) for the agent workflow,
enforced boundaries and recovery rules.

The grouped skill catalog is
[`src/archon_horizon/pipeline/skills`](src/archon_horizon/pipeline/skills).
Worker and reviewer subagent descriptions live separately in
[`pipeline/subagents`](src/archon_horizon/pipeline/subagents), grouped by specialty.
Read the [reviewer guide](docs/pipeline-reviewers.md) for skills, descriptors,
review attribution, and editing existing project configuration.

See the [documentation index](docs/README.md),
[architecture](docs/architecture.md), and [development guide](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE). Third-party attributions are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
