# Agent Context

A **context briefing** is a read-only view of the control plane's current records
for an assignment. Python assembles it from the database. It does not invoke a
model, start a planning session, or summarize the provider's transcript.

Three different things contribute to what an agent knows:

| Surface | Purpose |
| --- | --- |
| Startup instructions | Give a fresh session its mission, phase inputs, open ledger, role guidance, and pointers to relevant skills |
| Native session history | Preserve the provider's conversation and working context when the same session resumes |
| Context API | Let the agent refresh current shared records, ownership, notices, or diagnostics on demand |

Continuation instructions update retained work and completion findings. They
do not replace native history with the context API response. Prepared reviewers
receive their exact-revision review packet and lifecycle directly.

## Choosing A View

Inside a dispatched run, the execution environment supplies the credentials for:

```sh
horizon-pipeline agent context
horizon-pipeline agent context --view operations
horizon-pipeline agent context --view full
```

The default `brief` view includes the current assignment, mission and run,
coordination and control notices, and small collections of open ledger items,
pending notifications and active executions. Compact JSON is bounded to **32 KiB**;
this is a byte limit, not a token limit. Large fields and older records have
`detail_url`, `list_url`, and truncation indicators so the agent can fetch the
specific missing information.

The `operations` view is a **16 KiB** diagnostic snapshot of owners, queues,
admission reasons, review demand, and resource observations. It helps answer
questions such as why work is waiting or whether an interrupted owner still
holds capacity.

The `full` view includes more fields and up to 100 records in each collection.
It has no equivalent hard byte cap, and collection links remain necessary for
long histories. Use targeted record reads when only one item is needed.

Reading a view does not acknowledge a notification, settle a control notice,
or complete a ledger item. Those are separate attributable mutations. A read
can also become stale; mutations use record revisions to detect conflicting
updates.

The implementation is in
[context_briefing.py](../src/archon_horizon/pipeline/execution/context_briefing.py)
and [MissionService.context](../src/archon_horizon/pipeline/missions/service.py).
Prompt construction is separate in
[prompts.py](../src/archon_horizon/pipeline/instructions/prompts.py).

## Native Provider Capabilities

Horizon adds its mission, ledger, skills and API tools to the provider's ordinary
coding workflow. It preserves native conversation history and automatic
compaction. A context briefing does not replace either of them.

| Capability | Default behavior |
| --- | --- |
| Codex collaboration | Native subagents and the newer multi-agent mode are enabled; Horizon imposes no child cap unless configured. |
| Codex research | Live web search is enabled. A harness can select cached search or disable it. |
| Codex customization | The dedicated `CODEX_HOME` and project configuration supply native skills, plugins, hooks, MCP servers and memory settings. Native trust and account requirements still apply. |
| Claude tools | Writable container runs use the provider's default built-in tool set, including newly added tools. An explicit tool selection is honored. |
| Claude customization | Container runs load native user, project and local settings, including configured hooks, plugins and MCP servers. Horizon no longer forces an empty configuration. |
| Claude background helpers | Helpers can finish within Horizon's execution deadline; the native ten-minute idle waiting ceiling is disabled. |
| Native goals | Available according to the provider's configuration. They may mirror the authoritative Horizon mission and ledger. |

Install and configure extensions in the worker's dedicated provider home or
project workspace. The operator's personal home and shell environment are not
inherited. Enabling discovery makes configured extensions available; it does not
install or authenticate every plugin, connect an interactive desktop, or enable
every experimental provider flag.

The harness settings `codex_multi_agent_v2: false`, `codex_web_search: cached`
(or `disabled`) and `claude_native_configuration: false` provide explicit
overrides. Claude read-only runs keep the narrow read tools and suppress native
settings/MCP discovery. An explicit zero child limit removes Claude's Agent/Task
tools while retaining its other default tools.

Unattended runs cannot answer interactive permission prompts. Normal container
profiles use preauthorized provider execution inside rootless isolation; host
runs retain their selected permission and sandbox policy. Execution deadlines,
queue/provider quotas, container resource limits and scoped API permissions still
apply. The default worker permits one hour per provider request, four hours per
execution episode and 16 provider requests per episode. Unfinished work resumes
its retained session according to the scheduler's recovery policy. These bounds
govern supervised execution and recovery. Historical legacy supervisor runs
retain their read-only, zero-child and five-minute request policies; new launches
cannot select that retired profile.

This audit checked the CLI surface of Codex `0.153.4` and Claude Code `2.1.293`,
without launching authenticated model sessions. Codex's feature listing reports
the newer multi-agent mode as stable; its configuration accepts live web search.
The official OpenAI documentation endpoint was unavailable from the audit host
(HTTP 403), so these Codex observations are specific to the checked executable.
Claude's [CLI reference](https://code.claude.com/docs/en/cli-reference) and
[programmatic usage guide](https://code.claude.com/docs/en/headless) explain
settings discovery, tool selection and background waiting. Verify the pinned
provider release when enrolling a different version.
