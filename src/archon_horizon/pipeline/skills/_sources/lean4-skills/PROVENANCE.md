# Lean Skill Sources

The Horizon skills below adapt selected guidance from
[cameronfreer/lean4-skills](https://github.com/cameronfreer/lean4-skills),
copyright Lean 4 Theorem Proving Skill Contributors, under the
[MIT license](LICENSE.md).

Source revision: `4400419ba345fd0dc07f278359b372505780ef71` (2026-09-28).
Reviewed for this adaptation on 2026-09-29. Source files are under
`plugins/lean4/skills/lean4/references/` at that immutable revision:

| Horizon Skill | Upstream References | Adaptation |
| --- | --- | --- |
| `lean-search` | `mathlib-guide.md` | Local/version-aware search; verify candidates in the actual goal; optional LSP or shell fallback |
| `lean-proof-repair` | `compiler-guided-repair.md`, `performance-optimization.md` | Diagnostic-driven repair, managed build admission, no fixed retry count or model requirement |
| `lean-library-engineering` | `mathlib-style.md`, `proof-refactoring.md`, `performance-optimization.md` | Destination-sensitive conventions, API reuse, measured performance, no fixed line-count thresholds |
| `lean-cycle` | `cycle-engine.md` | Autonomous evidence-driven cycles with Horizon ledger and continuation, no interactive approval loop |
| `lean-refactor` | `proof-refactoring.md`, `proof-simplification.md` | Mathematical helper boundaries and coherent consumer migration, no line-count mandates |
| `lean-performance` | `profiling-workflows.md`, `performance-optimization.md`, `instance-pollution.md` | Controlled measurements, version-aware options, managed builds, no claimed universal speedups |
| `lean-proof-golf` | `proof-golfing.md`, `proof-golfing-patterns.md`, `proof-simplification.md` | Checked candidate transformations, clarity and directness, no speculative success percentages |
| `lean-simplifier` | `simp-reference.md` | Local/global normal forms, rewrite-policy tests, version-aware simproc principles |
| `lean-counterexample` | `disprove-engine.md` | Preserve target, separate certified negation from candidate witness and inconclusive search |
| `lean-tactics` | `tactic-patterns.md`, `calc-patterns.md`, `proof-simplification.md` | Goal-directed strategy, actual post-simplification context, optional tools only |
| `definition-quality` | `instance-pollution.md`, `mathlib-review-taxonomy.md` | Witness consumers, canonical representations, explicit ambient instances |
| `proof-review` | `axiom-elimination.md`, `mathlib-review-taxonomy.md` | Exact exported-name inspection, explicit trust policy and coverage, source fidelity distinct from axiom closure |

The catalog also restores Horizon's own statement-alignment, definition-quality,
proof-review, proof-cycle, checking, and contract-first refactoring procedures.
They use the current runtime's managed builds, publication receipts, reviewer
descriptors, and mission ledger. Removed API versions, host helper variables,
mandatory per-node reviewer dispatch, and legacy label protocols are not retained.

These are edited adaptations, not a full upstream plugin installation. They
include no upstream hooks, slash-command runtime, shell wrappers, credentials,
subagent launcher, or automatic external reporting. They work with the tools the
current Horizon harness exposes. No startup download or package installation is
needed, and no upstream update changes an already pinned run bundle.

The adaptations deliberately omit universal specialization advice, illustrative
performance multipliers, mandatory interactive checkpoints, and assumptions that
a named MCP tool is installed. Horizon's mission, mathematical target, build
admission, publication policy, and continuation rules remain authoritative.

When updating, compare the listed files at an explicit new commit, review the
changes for provider assumptions and mathematical correctness, update this
record and preserve the license. Keep focused references local so an agent can
read them without downloading a plugin. The official mathlib documentation and
the destination's pinned source remain the authority for concrete API and style
questions.
