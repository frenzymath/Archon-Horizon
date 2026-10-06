---
name: definition-quality
description: Evaluate Lean representations and foundational definitions using real consumers, canonical APIs, definitional equality, useful bundling, and coherent instances.
metadata:
  category: review
---

# Definition Quality

Start from the mathematical object and witness consumers. Read the definition,
its constructors/projections, instances, and a typical theorem using it. If
the concern is performance, compare the defining module with consuming modules
and retain a minimal slow or failing example. One difficult proof does not
establish that a representation is wrong.

Search local and pinned library analogues with
[lean-search](../../lean/lean-search/SKILL.md). Compare the exact type and its
use, not only its name. Evaluate:

| Question | Useful evidence |
| --- | --- |
| Does it represent the intended object? | Source definition, construction, and bridge to canonical objects |
| Does bundling help? | Consumers need these fields together; projections replace repeated reconstruction |
| Is the API reusable? | A natural independent consumer with ordinary hypotheses and predictable naming |
| Does it elaborate well? | Ordinary `exact`, `rw`, and `simp` work without repeated transports |
| Are instances coherent? | Actual selected instances, diamonds, search traces, and namespace scope |
| Are assumptions honest? | Required proof fields have producers from the intended hypotheses |

An `abbrev` or structure is not intrinsically poor. Repeated alias wrappers,
equivalent nested structures, and context reconstruction become concerns when
they cause demonstrated consumer complexity. Avoid constructing an elaborate
general hierarchy for hypothetical users; remove cheap accidental
specialization when the current mathematics already supports it.

In postprocessing, make a demonstrated low-cost improvement a review finding
even if existing consumers compile. Compare the smaller natural signature,
weaker existing typeclass, conventional name or shared definition with the
current design; explain the reuse or complexity benefit and check affected
consumers. The maintainer should implement the bounded repair or document why
its migration cost or mathematical tradeoff outweighs that benefit. This does
not require an unbounded abstraction or a rewrite for personal taste.

Read [representation experiments](references/representation-experiments.md)
when deciding between repairs. Prove one representative consumer against a
smaller candidate API in scratch before recommending a rewrite. Distinguish a
missing bridge lemma, poor reducibility, an instance conflict, and a genuinely
wrong mathematical definition; these require different repairs.

For a review, report the definition, consumers, searched analogues, mechanism,
smallest tested alternative, and remaining uncertainty. For an authorized
edit, use [lean-refactor](../../lean/lean-refactor/SKILL.md) to migrate the
contract and consumers together. A reviewer descriptor supplies perspective
and permissions; this skill supplies the investigation procedure.
