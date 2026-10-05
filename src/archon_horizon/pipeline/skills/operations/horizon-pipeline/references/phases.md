# Phase Outcomes And Acceptance

Read only the section for the current run. The phase inputs identify the source
and destination repositories; current repository policy supplies exact review
requirements. These are three uses of the same work-and-review loop.

## Preprocessing

The deliverable is an accepted roadmap: meaningful milestones and dependencies,
their Lean statements, and the definitions needed to interpret them. It is not
a proof of the whole project. A useful split exposes intermediate results and
interfaces; creating more nodes or sessions is not itself progress.

Workers propose changes in the roadmap repository. For milestone projects:

1. Propose the named route, source alignment and milestone decomposition. Review
   its mathematical coverage, bridges and dependency order.
2. Provide the Lean contracts and concrete supporting Definitions modules.
   Theorem proof bodies may be admitted in this phase; definitions and their
   types must not hide missing content behind admissions or assumed endpoints.
3. Maintainers obtain the required current-head statement-fidelity, definitions,
   decomposition and library-api assessments plus trusted contract verification.
   Request specific repairs in the existing PR and reuse still-applicable evidence.
4. Merge the accepted contracts, index the merged roadmap revision, and prepare
   the approval packet with checks matching that exact source and manifest.
   The root maintainer closes preprocessing once this evidence and its scope are
   complete. Human baseline approval is a separate operator action.

Use [milestone operations](../../horizon-graph/references/milestones.md) for the
actual routes, manifest fields, trusted verification and packet procedure.
A verification request returns a milestone job ID; a completed job links its
check receipt. These IDs are not interchangeable. If waiting, use the job's
completed/failed event, keeping one result owner.

Human approval freezes the initial baseline used by a separately launched
formalization run. It is not a reason to keep preprocessing sessions alive.

## Main Formalization

The shared workspace is the primary working repository. Workers prove accepted
milestones and useful intermediate results there, publish work and maintain the
graph's source-bound proof evidence. There is no routine worker-proof PR gate on
the workspace. The reviewed repository remains the roadmap: maintainers review
graph changes, mathematical progress claims and proposed contract corrections.

Use the adopted baseline to keep milestone meanings stable. Nodes may acquire
helpers with explicit milestone ownership; the graph records the dependencies.
A consumer proved using an admitted prerequisite is conditional, even when Lean
checks the consumer. Completed proof claims need the applicable trust evidence
and dependency closure.

A changed statement or definition is a contract change, not merely proof progress.
It needs the strict contract review and explicit baseline adoption process.
Keep proving independent unchanged work while a concrete contract issue is resolved.

The root maintainer closes this phase when the mission's required proof outcomes
are established, conditional gaps are accounted for, and the graph points to the
actual published evidence.

## Postprocessing

The source workspace contains existing formalization; the destination library is
the reviewed repository. Workers inspect a coherent family of source results,
reuse sound code, adapt public interfaces where needed, and open focused library
PRs. Remake code when its mathematical meaning or design warrants it, not because
the source is external to the library.

Review public statements and definitions before spending effort on proof style.
Look for faithful meaning, unnecessary hypotheses, useful generality, compatible
representations, naming, imports and downstream reuse. Validate in the destination
package, starting with changed modules and representative consumers. Reuse an
exact-input check only when source, manifest, toolchain and target still match.

A separate statement skeleton is useful if an unsettled interface would cause
substantial rework. An existing sound proof can be ported with its interface.
Where destination policy permits an admitted skeleton, preserve a clear proof
obligation and never describe it as a completed theorem.

Maintainers apply the destination policy to each current head. Every required
dimension needs fresh specialist evidence or an explicit, permitted carry-forward
supported by unchanged applicable evidence. Earlier objections require explicit
resolution; a clean build or generic approval cannot erase them. Use
[horizon-review](../../../review/horizon-review/SKILL.md) for the reporting contract.

Use the graph as an integration map. Workspace proof completion and library
completion are separate repository-scoped facts. Update library mappings as
results land, without making a full graph reorganization a prerequisite for a
ready port. A bounded family can produce several PRs; helpers do not each require
a separate mission.

The root maintainer closes the phase when the requested library scope is
integrated and any intentionally remaining gaps are explicit. Shared architecture
concerns can justify a bounded independent audit; it is not a recurring stage.

## Closure And Authority

Workers deliver results; reviewers deliver assessments; maintainers decide
acceptance. These are different completion criteria. Complete fulfilled child
missions from the leaves upward within your authority. The root maintainer
records the semantic result and drains the run using horizon-report. Reuse
accepted evidence for closure rather than adding an end-of-run re-review.
