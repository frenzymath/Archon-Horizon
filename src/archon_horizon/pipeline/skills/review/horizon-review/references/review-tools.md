# Choose Evidence Tools

Start with the exact question and existing artifacts. A tool supplements review;
it does not decide mathematical fidelity or merge authority. Discover the tools
actually available in the execution. Never assume a descriptor installs a plugin,
an external service, a benchmark runner or credentials.

| Question | Evidence/tool | Limits |
| --- | --- | --- |
| Does this statement match its intent? | Exact source, elaborated types, definition bodies, independent translation/counterexample | A green build cannot decide fidelity |
| Is there a reusable result? | Pinned source search, available Lean search tools, actual signatures | A name match or search miss is not conclusive |
| Does a proposed replacement work? | Narrow LSP/Lean probe in the real imports and toolchain | An untested snippet is a suggestion |
| Is the endpoint trustworthy? | Exact-declaration axiom inspection; project's configured comparator/kernel checks | Text scans and a helper audit do not cover the endpoint closure |
| Do modules compose? | Import/dependency inspection and representative combined consumers | One file's checks omit downstream global effects |
| Did performance regress? | Existing Radar/CI measurements, then focused profiling under comparable conditions | Missing measurements, cache differences and contention prevent a confident comparison |
| Can users build the release? | Configured CI targets and managed `lean-check` | Cached artifacts must correspond to the published source |

For upstream precedent, make a focused search tied to an unresolved design claim.
Inspect the actual accepted code/discussion and its revision; describe material
differences. Do not infer policy from a rejected PR's title or invent a lemma.

## Radar And Benchmark Evidence

[Radar](https://github.com/leanprover/radar) is a benchmark server/runner system.
The [public instance](https://radar.lean-lang.org/) includes configured projects;
it does not automatically benchmark every Horizon library. Consult its existing
commit/PR results when they cover the question. Pin both source commits, run
schema, benchmark revision, runner, toolchain and dependency/cache conditions.
Inspect run success as well as values: Radar can retain measurements from a
failed run. Report absolute/relative deltas, units and repeatability.

The upstream GitHub bot recognizes `!bench` / `!radar` for repositories configured
on that service. These are external job requests, not universal local commands
or guaranteed Forgejo features. Use a project's documented trigger only when its
integration and benchmark budget authorize it, through the supported Forge
delivery path; retain the request/result reference and avoid duplicate requests.
Do not install Radar, provision tokens, or post commands to unrelated repositories
to obtain a review. An unavailable service calls for existing CI evidence or an
owned benchmark request with the exact target and missing capability.

For an unconfigured project, select its existing benchmark/profile procedure.
Mathlib's [benchmark suite](https://github.com/leanprover-community/mathlib4/blob/master/scripts/bench/README.md)
documents its own scripts and measurement output; their presence in a dependency
does not make running the entire suite appropriate for a small project change.
Use managed admission for Lean compilation. A separate benchmark runner must be
explicitly configured to respect host resources; the build helper is not a
generic arbitrary-script runner. Prefer dedicated or suitably quiet runners
for cost comparisons. Put large checkouts and generated data under `$TMPDIR`.

## Compare Existing Measurement Files

The bundled standard-library Python helper compares Radar-format JSONL files
produced by existing benchmark runs; it does not launch builds, contact a server,
authenticate or publish. Resolve the installed `lean-performance` directory via
`SKILLS.md`, then run its `scripts/compare_measurements.py`:

```sh
python /resolved/lean-performance/scripts/compare_measurements.py \
  --base /scratch/base-measurements.jsonl --head /scratch/head-measurements.jsonl \
  --base-commit FULL_BASE_OID --head-commit FULL_HEAD_OID \
  --context 'runner, benchmark revision, toolchain, targets, cache conditions'
```

Replace placeholders with actual paths and evidence. Each input row has `metric`,
numeric `value` and optional `unit`. Duplicate metrics in one run are summed,
as in Radar, but inconsistent units are rejected rather than silently combined.
Do not concatenate repetitions into one file; compare each matched pair so
variance remains visible. Missing/new metrics and cross-run unit changes have no
computed delta. A zero baseline has no percentage delta. Metric increases are
not automatically regressions: direction and materiality depend on the metric.

The JSON output labels commit/context metadata as caller supplied. Verify artifact
provenance and successful runs independently; matching filenames or supplied
hashes do not authenticate measurements. Retain the input artifacts with the
report. Exit 0 means valid comparison output, not that a performance gate passed.
Exit 2 reports invalid input. There is no automatic approval or threshold policy.

## When Evidence Is Unavailable

State the unavailable input/tool, affected claim and smallest useful next check.
Ask a build/performance specialist or queue an owned check if it needs another
host or must outlive the review. Continue independent inspection. Do not use
unmanaged full builds to evade admission, poll while consuming all capacity, or
interpret missing data as zero cost/success. Essential missing evidence gives an
incomplete review; optional investigation can remain a labeled follow-up.
