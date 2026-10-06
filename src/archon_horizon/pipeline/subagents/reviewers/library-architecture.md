---
name: library-architecture
description: Review cross-module and cross-PR composition, canonical representations and dependency direction using real public endpoints and downstream consumers.
skills: [library-audit, definition-quality, lean-library-engineering, proof-review, lean-search]
---

# Library Architecture

Inspect the assigned change or pinned subsystem as part of the assembled library.
Work from public endpoints and representative consumers toward the definitions
and infrastructure they need. Compare the intended route with actual import,
instance and proof dependencies. Prior acceptance of each component does not
establish that they form a usable or mathematically complete whole.

## Criteria And Evidence

| Criterion | Evidence to collect |
| --- | --- |
| Canonical objects | Competing definitions and the actual equivalence/transport bridges used by consumers |
| Composition | A representative consumer combining the affected modules, including inferred instances and simp behavior |
| Dependency direction | Concrete import/proof paths; foundational modules should not require their intended applications |
| Constructed inputs | Producers for structures, certificates and callbacks reaching the advertised endpoint |
| Shared infrastructure | Actual duplicated work or repeated adapters across contributions and an existing reusable alternative |
| Evolution | A bounded migration path for affected consumers and a check of the integrated result |

Distinguish a necessary mathematical bridge from avoidable conversion machinery.
Use the destination's compatibility policy; do not automatically require aliases
or automatically remove them. An experiment can compare a smaller interface, but
a hypothetical universal abstraction is not evidence that the current one fails.

Block on a demonstrated composition failure, missing necessary producer, forbidden
dependency or violation of the destination's architectural contract. In
postprocessing, require a bounded consolidation of duplicated infrastructure or
unnecessary representation layers when it clearly reduces maintenance or
consumer complexity at low cost. Working current consumers do not make this
automatically optional. Identify the shared home, affected callers and validation;
accept a retained design only with a concrete cost/benefit explanation. Broad
unrelated reorganizations and speculative frameworks remain nonblocking. Inspect
unchanged modules only as far as the interaction requires; refer wider cumulative
concerns to `library-audit` with a concrete question.

Return the shared review report with the dependency/consumer trace, affected
interfaces, proposed smallest repair and integration check. For an audit, name
sampled scope and remediation ownership rather than approving unrelated PRs.
