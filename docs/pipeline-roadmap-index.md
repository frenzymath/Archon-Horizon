# Roadmap indexing

The Forge knowledge repository remains authoritative. Horizon indexes committed
Markdown under `nodes/` and `objectives/`, preserving frontmatter labels, source
paths, dependency edges and objective text. Displayed labels are source claims;
indexing does not perform mathematical acceptance or create missions. Source
links point to the indexed commit, not a moving branch. The dashboard reports the
last index time.

Nodes support repository-scoped `implementations`, with identical progress and
review states for workspaces and libraries. See the
[agent contract](../src/archon_horizon/pipeline/skills/operations/horizon-graph/references/repository-progress.md).
The API validates target ownership, typed claims, immutable proof pins and each
target's dependency DAG before indexing. Content-bound evidence becomes stale
after node rewrites. Dashboard graph, directory and node views accept
`target_repository_id`; `/api/v3/roadmap` also accepts `run_id`. The active run's
destination is the default, with an explicit repository selector in the dashboard.
This is a Git source format and derived projection, not a second editable database.

Default graph projects treat milestones as ordinary nodes with the `milestone`
label. Agents choose names and additional metadata through roadmap PRs.
The graph displays milestones as stars, while color continues to show progress.
Historical `type: milestone` nodes receive the same marker. The **Nodes** page
combines text search, milestone membership, node type, and progress filters before
pagination. Types come from the project's indexed nodes, including custom types.
Directory API filters are `search`, `milestone=true` or `false`, `node_type`, and
`label`; progress is evaluated for the selected implementation repository.

For explicitly retained [milestone projects](pipeline-milestones.md), the index also retains bounded
Lean sources, Lake/toolchain pins and `milestones/**/*.md`. It validates typed
objective/milestone locators, objective-scoped IDs, ownership links and dependency
cycles without running Lean inside the API. Trusted build receipts and reviewed
human baseline approval are separate from indexing.

After applying migration `0016_source_projection`, preview an explicit import:

```sh
python -m archon_horizon.pipeline.roadmap_index \
  --config /absolute/server.json --operator operator \
  --repository-id REPOSITORY_UUID --git-directory /absolute/roadmap.git \
  --revision refs/heads/main
```

The preview validates the full graph inside a transaction and rolls it back.
Pass `--apply` to commit. The command never edits the source repository or any
another control-plane database. Missing dependencies, duplicate identities and cycles reject
the complete update, preserving the previous index. Catalog nodes retain their
identities by repository and path. Removed source paths are not deleted from
the catalog because missions may still reference them; their projection is
removed, and the dashboard reports them as not indexed.

For a local Forge bare repository, install the provided systemd service and
timer, adjusting the Python executable path and environment file. Set these
variables to explicit operator-controlled values:

```ini
HORIZON_SERVER_CONFIG=/absolute/server.json
HORIZON_OPERATOR=operator
HORIZON_ROADMAP_REPOSITORY_ID=REPOSITORY_UUID
HORIZON_ROADMAP_GIT_DIRECTORY=/absolute/roadmap.git
HORIZON_ROADMAP_REVISION=refs/heads/main
```

The timer reads the accepted branch every five minutes; an unchanged commit
skips parsing and writes. Systemd serializes the service and bounds its runtime.
Use one service instance per repository if more than one roadmap is configured.
A remote Forge needs a separately supervised mirror fetch into a dedicated
checkout before this indexer runs; worker checkouts are not authoritative.

Set `integration_public_urls` in server configuration to a mapping of integration
UUIDs to their browser-facing HTTPS URLs. Connector endpoints remain private.
Unconfigured loopback endpoints are never rendered as browser links. Zulip's
canonical external hostname and reverse proxy must match its configured public
URL.
