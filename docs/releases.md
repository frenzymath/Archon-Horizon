# Release Tags And Installation

## Current Alpha

Current source, the README badge and frontend metadata use `0.2.0-alpha.2`.
Python distribution metadata normalizes this prerelease spelling to `0.2.0a2`.
The `v0.2.0-alpha.2` tag identifies the reviewed, merged source snapshot with
objective orchestration, agent-led planning, optional subagent limits and
reference-file storage. Existing control planes must explicitly migrate through
`0031_reference_files` before restarting with this version.

For a reproducible source installation, select the tag before building:

```sh
git clone --branch v0.2.0-alpha.2 --depth 1 https://github.com/frenzymath/Archon-Horizon.git
cd Archon-Horizon
```

Then follow that checkout's installation instructions. A branch such as `main`
can move; a release tag names a fixed source commit. Alpha status remains in
effect regardless of development branch names or passing unit tests.

## Historical Tags

[v0.2.0-alpha.1](https://github.com/frenzymath/Archon-Horizon/tree/v0.2.0-alpha.1)
identifies published alpha commit `0df5571f81aae29130c8ec621c9fe7419ca28d6a`.
That historical source reports package version `0.2.0` and Alpha development
status. The prerelease tag makes its maturity explicit; it does not contain the
subsequent development changes.

This alpha snapshot
predates the new objective orchestration workflow and schema revisions 0028/0029.
It remains unchanged; later alpha work receives a new tag rather than moving an
already published one.

[v0.1.5](https://github.com/frenzymath/Archon-Horizon/tree/v0.1.5) identifies release
commit `5d603f8ee7d66de81eff2863cbb6f29b4440712a` from September 4, 2026. The
same-named historical branch includes a later follow-up commit; use the tag to
select the original release snapshot.

## Preparing Future Releases

Review and commit the intended source, update version surfaces with
`scripts/version.py`, run the checks in CONTRIBUTING, and build from a clean
snapshot. Verify wheel/source-archive contents, worker-only import isolation and
required operator migration instructions. A tag cannot include dirty files.

Use prerelease names while collecting beta evidence, and align future package
metadata with the release version. Tag the reviewed commit with an annotated
Git tag, then push that tag. Publishing a GitHub release or package is a separate
action. Do not label a snapshot stable merely because its unit tests pass.
