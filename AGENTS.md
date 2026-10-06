This repository implements **Archon Horizon**, the distributed control plane
for Lean formalization projects.

These instructions guide agents developing Horizon itself. Dispatched
formalization runs receive their instructions from the API's pinned catalog.

Before doing anything else, load the **`horizon-pipeline`** skill. When
`HORIZON_SKILLS_DIR` is set, resolve `horizon-pipeline` in
`$HORIZON_SKILLS_DIR/SKILLS.md` and read that skill
and follow that run's workflow. Otherwise read
`src/archon_horizon/pipeline/skills/operations/horizon-pipeline/SKILL.md` for the implemented workflow;
its run credentials, reports, and delegation APIs apply only inside a
dispatched run. See `CONTRIBUTING.md` for development setup and checks.

For implementation work outside a Horizon run, inspect the current code and
tests. Do not invent run credentials or submit formalization reports. Run
checks in the foreground and wait for them to finish. Preserve operator state
and unrelated working changes. Delegate only independently scoped work and
wait for its result before finishing.

Use disk-backed scratch for large snapshots, checkouts, and test artifacts.
`/tmp` is quota-limited on the deployed hosts; free workspace disk space does
not imply free temporary space. Inside a run use `$TMPDIR`. For development,
use a dedicated directory under `~/.horizon/development-tmp` and set `TMPDIR`
for commands that create large temporary files; clean generated artifacts when
finished while preserving source edits and review evidence.
