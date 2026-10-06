---
name: library-audit
description: Audit a pinned Lean library or subsystem for cumulative mathematical, API, instance, import and performance problems across accepted changes; produce bounded evidence and owned repairs.
metadata:
  category: review
---

# Audit The Composed Library

Use when a worker or maintainer has a concrete doubt about the assembled library, or a
mission explicitly requests an audit. Start from the current accepted commit,
the intended public endpoints and representative users. Independent PR approvals
do not establish global coherence. A whole-library claim requires whole-scope
evidence; label a subsystem audit or sample as such.

In source-to-library work, a short initial survey can identify clusters and
shared design risks without auditing every source file. A later broader audit
is useful when cluster work reveals repeated adapters, incompatible interfaces
or an implausible route to the main results. Scope the investigation to that
question and continue independent work; neither creating a graph nor finishing
a global audit is a prerequisite for porting an already working source cluster.

## Frame And Inspect

State the question, repository/commit, relevant modules, dependency pins,
acceptance criteria and scope/time limits. Identify the suspected interaction
and a consumer that would expose it. Inventory the relevant public interfaces,
imports, instances, simp lemmas and proof dependencies. Read accepted reviews
and rejected alternatives only where they explain these choices.

Trace a real route from foundational definitions to a principal theorem and an
independent consumer. Check whether constructions actually supply all required
inputs, whether representations have proved bridges, and whether modules compose
without duplicate notions, incompatible normal forms or surprising instances.
Inspect status/docstrings against transitive proof evidence. A sum of local green
checks may omit the combined import environment or final endpoint.

Use `horizon-review` to select perspectives dynamically. `library-architecture`
helps with composition; other reviewers can investigate a particular semantic,
API, trust or cost question. The optional `library-auditor` native specialist
coordinates one bounded investigation. Avoid recursively creating auditors with
the same open-ended task; split only independent questions with clear inputs.

## Execute Through Existing Ownership

With no PR, use a native child and collect its report, or create an ordinary
queued worker assignment when the audit must outlive its parent. Supply the
question, exact commit, relevant descriptor and skill, permitted scratch area,
expected evidence and integration owner. The prepared reviewer-invocation API
is for a real registered Forge item; do not invent a PR/head to obtain attribution.
The parent records the audit in its handoff and links an existing or new Forge
issue when durable discussion/repair is needed. Follow `horizon-forge` and
`horizon-report` for publication and ownership.

Review read-only snapshots. Run experiments in permitted isolated scratch and
use managed build admission. Consult the `horizon-review` tool guide for existing
CI, Radar measurements, local probes and unavailable capabilities. An audit does
not authorize a dependency upgrade, mass rewrite or infrastructure deployment.

## Decide And Hand Off

Apply the shared `horizon-review` report contract with an audit conclusion rather
than a PR approval. For each demonstrated systemic problem provide:

- Exact affected declarations/modules and the interaction, with a consumer,
  dependency trace, counterexample or comparable measurements that exhibits it.
- Which prior/local checks missed the interaction and the scope of that inference.
- A bounded remedy, compatibility consequences and the integration check needed
  to establish that the components now work together.
- Immediate affected PRs/endpoints versus independent historical repair work.
- Proposed repair order, reusable evidence and one integration owner; distinguish
  proposed tasks from assignments actually queued and accepted.

Request further investigation only for material remaining uncertainty. Once the
bounded question is answered, return the result and preserve missing evidence
with an owner. Repairs are separately scoped work, followed by targeted
revalidation of the integrated result. Do not reopen every old review or hold
unrelated work indefinitely while seeking a perfect library.
