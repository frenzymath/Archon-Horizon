# Select Reviews From The Question

The maintainer owns the final decision. Shipped descriptors group compatible
concerns and describe the project's review dimensions. For post-processing,
every enabled descriptor linked to the repository policy defines a required
dimension. Cover changed concerns with fresh specialist review and materially
unchanged concerns with explicitly recorded reuse of an earlier specialist
approval. A maintainer-only assessment without that evidence does not replace
coverage. In milestone projects, route PRs require decomposition approval and
contract PRs require statement-fidelity, definitions, decomposition and library-api
approvals plus trusted checks, including corrections during formalization.
Verified graph-only changes and legacy roadmap workflows use proportional selection.

The [phase workflow](../../../operations/horizon-pipeline/references/phases.md)
gives the expected starting perspectives for each phase. For a foundational
library skeleton, address statement fidelity and definition/API quality before
demanding completed proofs. A direct repair is allowed; inspect its delta and
dependencies to determine which earlier conclusions remain valid.

## Choose An Initial Perspective

| Trigger | Perspective and question |
| --- | --- |
| New/changed roadmap statement | `statement-fidelity`: would proving this establish the intended mathematics? |
| Foundational representation | `definitions`: does it model the object and support real constructors/consumers? |
| Roadmap decomposition | `decomposition`: do the bridges reach the target without circular or ownerless gaps? |
| Routine roadmap progress | `roadmap-consistency`: does changed evidence justify the claimed status? |
| Published library theorem | `mathematical-fidelity`: do statements and actual proof inputs establish the advertised endpoint? |
| Exported API/reuse/naming/generality | `library-api`: can ordinary consumers find and compose canonical interfaces? |
| Cross-module or cross-PR interaction | `library-architecture`: do individually plausible components fit together? |
| Proof route/trust/automation | `lean-proof-quality`: is the proof trustworthy and maintainable using available results? |
| Cost regression/shared automation | `lean-performance`: what changed under comparable measurements, including consumers? |
| Imports/build/packaging/CI | `repository-quality`: can a user reproduce and consume the intended library? |
| Sources/docstrings/blueprint | `scholarly-quality`: can the reader trace faithful claims to declarations and sources? |

Naming, placement, generality, reuse and attribution remain explicit criteria
within these perspectives. Split them into narrower project descriptors when
their workload or independence warrants it, rather than creating one agent for
each style rule. Conversely, combining concerns in one prompt must not silently
omit them. Register and enable custom formal reviewer descriptors in the project
before preparing an attributed invocation. A post-processing policy's reviewer
list defines required coverage; any enabled project descriptor outside it can be
selected for an additional relevant question.
Native advisory helpers can use a bounded
custom prompt through `horizon-delegation`.

## Adaptive Rounds

1. Read the exact diff, intended outcome, destination policy and existing threads.
   Identify what is unknown and what existing checks already establish.
2. Dispatch independent questions with exact inputs and a requested result. Do
   not prime the child with a desired verdict. Use the invocation procedure for
   formal PR reviews and the ordinary delegation procedure for other audits.
3. Read the evidence, not only the verdict. Accept a clean scoped result when
   supported. Ask the same reviewer to clarify an unsupported claim, or request
   a fresh investigation when independent reasoning would resolve a real doubt.
4. Add a perspective only when it answers a new material question. For example:
   API review discovers repeated conversions across three modules; ask architecture
   for a common consumer and canonical representation. Performance review finds
   instance-search cost; ask definitions about the instance design after a
   reproducer exists. Source fidelity exposes a missing hypothesis; settle that
   before asking for shorter proofs of the old statement.
5. Resolve disagreements, inspect fixes and recheck the affected delta. Stop when
   the needed scope has sufficient evidence, or record the missing input and
   durable owner. Do not commission reviewers until one agrees with a preference.

The final maintainer decision explains substantive findings accepted, withdrawn
or deferred and why the current head meets policy. Missing essential review or
validation is not equivalent to an advisory suggestion that can be ignored.
In postprocessing, a concrete cheap improvement to public definitions, signatures,
names or structure needs repair or a justified tradeoff, not dismissal merely
because existing callers pass. Maintainers should make bounded repairs directly.
Account for every required dimension. Use the Forge protocol's explicit
carry-forward record for unchanged prior specialist evidence, and concise fresh
delta assessments for affected concerns. A changed definition, theorem contract,
or relevant dependency invalidates reuse of the affected conclusion.

## Cumulative Audits

Commission `library-audit` when evidence crosses PR boundaries: parallel APIs for
the same object, mutually troublesome global instances, an endpoint requiring an
unconstructed bridge, repeated cleanup reversals, or gradually growing import/
build costs. Pin the current accepted commit, the suspected mechanism and the
modules/consumers to inspect. Prior reviews and PRs are useful provenance, not
proof that the composition works. Broaden the scope only as findings justify it.

Choose an integration owner for repair. A pending PR can be blocked by a demonstrated
interaction it introduces or a prerequisite necessary to its claims. Unrelated
historical issues get their own repair scope; a global cleanup wish is not a
reason to freeze the queue. Recheck the integration result as well as each fix.
