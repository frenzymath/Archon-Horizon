# Corpus-Derived Formalization Review Guidance

This note records the review lanes derived from the Poincare corpus and active
Horizon workspaces. Horizon keeps a bounded set of focused skills and read-only
reviewer roles rather than one omnibus reviewer or one role per issue. The
guidance is deliberately advisory: Horizon remains a free agent, and the main
agent chooses which lanes are worth the cost.

## Corpus and method

The initial corpus was the public review history of
[`frenzymath/Poincare-Conjecture`](https://github.com/frenzymath/Poincare-Conjecture):
issues and issue records for #1--#18, their available comments, the formalization
review attachments, and the related repair commits and generated audit notes.
The records include the Hopf--Rinow and Morgan--Tian reviews, the connection/API
repair PRs, and CI/build-audit work. There were no separate GitHub pull-request
review comments available through the initial API pass: the snapshot contained
18 issue records (six PR records) and 34 issue comments. This is a useful but not
exhaustive sample. It is evidence for general questions, not a claim that every
project has the same defects.

The recurring vocabulary was broad: `source`, `bridge`, `proof`, and
`hypotheses` appeared across many reports, with repeated findings about
regularity, `\leanok` attachments, builds, `sorry`/axioms, consumers, corners,
instances, and stale documentation. The important pattern was not any one
theorem; it was that syntactic evidence was repeatedly mistaken for mathematical
evidence.

## Active-workspace corroboration

The current workspace logs were used as a second, independent sample rather
than as a source of project-specific rules. OpenGA-Horizon reports show genuine
incremental Lean progress alongside unresolved conditional endpoints, repeated
`queued`/no-terminal rounds, broad staging outside an explicit add set,
generated hgraph churn, missing provenance or source artifacts, and lock/queue
pressure. LeanAlgebraicGeometry-Horizon reports similarly separate a passing
module check from root reachability, unlinked or empty graph nodes, cross-project
ownership mistakes, stale roadmap references, repeated requeues, and build/index
contention. NCS runs add quota and interrupted-integration cases. These signals
support distinct `progress-integrity`, `run-health`, `verification-integrity`,
`graph-traceability`, `provenance-integration`, and `strategy` lanes; they are
not evidence that every task needs all of them.

Palimpsest prompt roles (`mathematical-correctness`,
`maths-lean-correspondence`, `lean-quality`, `architecture`, `documentation`,
and `auditor`) provided a useful independent vocabulary. Horizon adopts the
stable questions while dropping project-specific posting gates and fixed phase
ordering. Project-local page transcribers, chapter mergers, and source-specific
auditors remain overrides rather than global built-ins.

## Recurrent lenses

| Recurrent signal | What a reviewer should ask | Horizon home |
| --- | --- | --- |
| Source/Lean shape drift | Do objects, domains, quantifiers, regularity, hypotheses, conclusion polarity, and intrinsic/model status match the cited source? | `source-fidelity`; `source-fidelity-reviewer` |
| Attachment and provenance drift | Does `\lean`/`\leanok` name the terminal source-shaped declaration? Was the source read? Are statement and proof dependencies distinct? | `blueprint-integrity`; `blueprint-integrity-reviewer` |
| Equivalent or duplicate representations | Are coordinate, proxy, wrapper, and intrinsic definitions clearly named and connected by an equality/iff bridge with exact hypotheses? | `api-composition`; `api-composition-reviewer` |
| Hypothesis and edge-case failures | Could regularity, endpoint, corner, zero/origin, empty, boundary, or quantifier polarity make a predicate vacuous or alter the theorem? Does a deletion/minimality probe change anything? | `semantic-adversarial`; `mathematical-correctness-reviewer` |
| Verification overclaim | What target/import/dependent closure was actually built? What do diagnostics, warnings, `sorry`/axiom provenance, and rendered/graph checks establish separately? | `verification-evidence`; `verification-integrity-reviewer` |
| API and narrative drift | Is there an existing reusable API? Are instances, names, docs, imports, options, and downstream users still coherent? | `api-composition`; `api-composition-reviewer` |
| Proof and implementation quality | Are proof scripts, diagnostics, imports, names, docs, and downstream users maintainable without changing the statement? | `lean-quality`; `lean-quality-reviewer` |
| Graph/status mismatch | Are source boundaries, node status, `\uses`, and actual declaration consumers visible and honest? | `graph-traceability`; `graph-traceability-reviewer` |
| Process and workspace drift | Are repeated runs, staging, ownership, locks, queues, and reports consistent with the claimed progress? | `progress-integrity`; `provenance-isolation`; `run-health` |
| Epistemic/anti-evasion drift | Is a certificate a proved producer or only a conditional/empty package? Are reports counting wrappers, aliases, stale checks, or repeated rounds as progress? | `honesty`; `honesty-reviewer` |

The clustering is grounded in distinct review episodes, not only keyword hits:

- [Issue #5](https://github.com/frenzymath/Poincare-Conjecture/issues/5) and
  [Issue #11](https://github.com/frenzymath/Poincare-Conjecture/issues/11) show
  chart/local definitions standing in for intrinsic source concepts and the
  later need for additive connection and equivalence layers.
- [Issue #7](https://github.com/frenzymath/Poincare-Conjecture/issues/7) and
  [Issue #18](https://github.com/frenzymath/Poincare-Conjecture/issues/18) show
  that a compiling declaration or `\leanok` marker can still be the wrong
  statement, proxy, or terminal attachment.
- [Issue #16](https://github.com/frenzymath/Poincare-Conjecture/issues/16) and
  [PR #17](https://github.com/frenzymath/Poincare-Conjecture/pull/17) expose
  incomparable hypotheses, model-versus-manifold claims, and origin/edge-case
  failures that require calibrated review rather than a binary pass.
- [Issue #12](https://github.com/frenzymath/Poincare-Conjecture/issues/12) and
  [PR #1](https://github.com/frenzymath/Poincare-Conjecture/pull/1) show the
  separate API-quality and build/axiom-integrity dimensions.

These are dimensions, not a mandatory checklist. A small local lemma may need
none of the source or graph questions; a source-backed exported definition may
need several. Review reports should use calibrated outcomes such as
`satisfactory`, `partial`, `mismatch`, `unverified`, or `needs adjudication`,
with scope and evidence, instead of forcing a binary pass/fail.

The anti-evasion vocabulary is also informed by the public
[auto-formalizing trap catalog](https://github.com/qinz1yang/auto-formalizing-skills/blob/main/prove/prove-TRAPS.md)
and its [proof protocol](https://github.com/qinz1yang/auto-formalizing-skills/blob/main/prove/prove-PROTOCOL.md).
Horizon does not copy that project's prompts or fixed phase workflow. It keeps
the transferable tests—binder/quantifier and domain drift, model-versus-source
substitution, hypothesis packaging, root reachability, transitive axiom
provenance, and a stop rule for repeated unchanged frontiers—in the focused
honesty and strategy lanes.

For machine or mass scanning, reports begin with the shared `Status` token
(`satisfactory`, `partial`, `mismatch`, `unverified`, or `needs-adjudication`).
Lane-specific outcomes such as `converging`, `healthy`, or `reproducible` belong
on a separate `Verdict` line; findings use `blocker`, `major`, `minor`, or
`note` severity and always carry evidence and a next action.

## Focused skills and roles

The colleague's source-faithfulness proposal supplies the highest-value core:
formalize the statement the source makes, treat equivalence as a theorem, bridge
duplicate concepts, and disclose proof-route hypotheses. That material belongs in
`source-fidelity`, while edge cases, progress, proof load-bearing, consumer
routes, verification, graph traceability, API composition, and provenance have
their own focused skills.

The motivating covariant-derivative/geodesic example is representative: keep the
intrinsic source-facing declaration as the anchor, and expose a chart ODE or
computational formula as a named model with an explicit bridge when one is
proved. The same distinction recurs in the Poincare-Conjecture reviews.

The writable `blueprint` helper remains an author/owner. Independent
`source-fidelity-reviewer` and `blueprint-integrity-reviewer` roles prevent an
author from certifying its own attachment. `work-reviewer` remains a narrow
progress-integrity pass: it checks whether the task is advancing honestly,
avoiding loops and performative work, but does not certify theorem meaning,
kernel soundness, graph correctness, or API quality. `ground` remains strategic
and workspace-wide; `janitor` remains operational hygiene.

The initial bundled reviewer roster is deliberately bounded and separated:
`source-fidelity-reviewer`, `mathematical-correctness-reviewer`,
`blueprint-integrity-reviewer`, `proof-load-bearing-reviewer`,
`api-composition-reviewer`, `verification-integrity-reviewer`,
`graph-traceability-reviewer`, `provenance-integration-reviewer`, and
`strategy-reviewer`, plus the focused `consumer-dependency-reviewer` and
`honesty-reviewer`. Run-health
is bundled as a read-only operational role because active work repeatedly shows
queued/no-terminal, lock, quota, and staging failures.

`lean-quality-reviewer` is bundled: the active API/cleanup reviews show
that proof robustness, diagnostics, documentation, and downstream compatibility
are useful questions but should not be folded into mathematical correctness or
progress review. `mathlib-orientation` is a skill/resource map rather than a
second search agent. `honesty-reviewer` is bundled as the cross-layer
anti-evasion lane. It classifies proved, conditional, imported,
axiom/sorry-backed, empty/vacuous, and unverified certificates; checks for
hypothesis packaging and stale evidence; and compares the first unmet producer
across recent rounds. A conditional certificate may be valuable, but it cannot
justify an unconditional `\leanok` or completion claim. Two rounds with the
same source-facing producer and no decrease in empty/conditional obligations
are reported as churn, even when each commit compiles. The lower-frequency `external-boundary-reviewer`,
`transcription-fidelity-reviewer`, `release-reproducibility-reviewer`, and
`review-adjudicator` profiles are bundled as read-only options too, but should
be dispatched only for those claim types; project-specific source mergers may
still override them.

## External source map

The shared `source-discovery` skill gives the lead and source-acquisition roles
a common map for evidence outside the current Lean checkout. It starts with the
workspace `references/` library and pinned project files, then widens to the
source repository's exact GitHub ref, Mathlib's checked-out source and upstream
PR history, Tau Ceti review artifacts, and Zulip maintainer discussions when
they answer a question of intent or convention. GitHub issues/PRs, Tau Ceti
rubrics, and Zulip messages are recorded as maintainer or review context; they
do not replace the primary mathematical source or a kernel check.

The acquisition role records stable URLs, commits, message locators, timestamps,
and access limits. Zulip may be read through an approved API credential or, when
explicitly permitted, a Playwright/MCP browser session. Credentials, cookies,
private message bodies, and session artifacts are never copied into reports or
the repository. An inaccessible or mutable discussion is an explicit
`unverified` boundary, not a reason to guess. `references/manifest.yaml` remains
the inventory for source files; graph comments and review reports hold the
scoped interpretation.

## Free-agent boundary

There is no runtime review gate and no automatic semantic-review dispatch. The
Horizon skill presents risk-triggered options, while the subagent skill says
explicitly that the roster is a set of choices. An agent may skip, combine, or
replace a review and may create a workspace-specific helper; it should record
the reason when that choice matters. This preserves the lightweight Horizon
architecture while making independent review discoverable at both writing and
review time.

## Maintenance loop

Future review reports can carry a small set of labels such as `fidelity`,
`bridge`, `hypotheses`, `edge-case`, `evidence`, `attachment`, `api`, and
`graph`. Periodically promote a pattern into the skill only when it is
recurrent, generalizable beyond one theorem, and actionable in a short prompt.
Keep the original report as evidence and link it; do not turn every issue into a
new skill. When a descriptor or advisory phrase changes, update its focused
contract tests and inspect generated Claude/Codex descriptors. Re-evaluate the
need for a specialist after several independent projects, not after one noisy
review.

The skill and descriptors intentionally say what to investigate and why, while
leaving the order, tools, depth, and stopping point to the agent. That is the
appropriate standard for a harness whose purpose is to improve judgement rather
than prescribe a second orchestration pipeline.
