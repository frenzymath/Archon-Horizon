# Search Routes

Start with a concrete uncertainty: name, type, abstraction, theorem strength,
or source provenance. Use the smallest scope that can answer it, then broaden
when the result is empty or irrelevant.

| Uncertainty | Search route | Verify |
| --- | --- | --- |
| Likely known name | `rg` on namespace/operation and local declaration search | Exact signature and import |
| Known conclusion shape | Available type-pattern tool or related consumer | Implicit arguments and side conditions |
| Mathematical concept only | Semantic search plus neighboring module tree | Actual declaration in pinned checkout |
| Repeated custom infrastructure | Canonical structure, constructor, extensionality and composition API | Representative consumer compiles |
| Historical design choice | Relevant repository history/PR discussion | Current source and destination conventions |

Try alternate mathematical formulations. Compactness might be expressed through
a finite subcover, closedness plus boundedness in a particular space, or a
sequential criterion with additional hypotheses. A local filter statement may
be the reusable form of an epsilon-delta lemma. A result about a structure may
already exist at a weaker algebraic level. Avoid assuming all equivalent prose
formulations are equivalent under the project's exact hypotheses.

Read neighboring declarations and their use sites. A promising name can be a
special case of a more suitable theorem nearby. Walk directories when an index
misses notation or a concept named differently; an empty index query is not a
proof the library lacks the concept.

Use horizon's published repository index for cross-repository discovery and
inspect its revision. Use local source for unpublished work. If an external
library is relevant, inspect a pinned revision and its toolchain compatibility
before proposing a dependency. A theorem visible on a current website may not
exist in the assigned mathlib commit.

Validate with the actual Lean type: `#check`, `#print`, hover, or a small
application in the target context. Confirm universes, explicit/implicit
binders, coercions, regularity, and instances. A discovered declaration is
only useful after this check; do not build a long proof around a guessed name.

Keep a concise useful result or failed route in current context so a resumed
session can avoid repetition. Do not turn every search into a Zulip message or
permanent source comment. Search should unlock proof work.
