# Extraction And Migration

## Choose a useful boundary

Inspect proof states around mathematical transitions: a bound, existence
witness, measurability/integrability result, induction invariant, or conversion
between representations. A helper earns its place when it captures a reusable
fact, separates substantial mathematical domains, removes meaningful repeated
reasoning, or gives the elaborator a stable typed boundary.

Do not split solely by line count. A clear long induction may be better kept
together; a short repeated conversion may deserve a named lemma. A helper
with many proof-specific parameters and inherited local `let` bindings can be
harder to use than the original `have`. Check its public type without relying
on the surrounding section context.

Keep implementation-only helpers private or local when no independent API is
needed. State reusable lemmas with the weakest natural assumptions supported
by the argument. Avoid generalizing to an abstraction whose added complexity
exceeds its actual use. Namespace and name the mathematical content rather
than calling the lemma `step3` or embedding the current project's headline
theorem name in every reusable fact.

## Preserve proof behavior

Moving a proof out of a section can change inferred variables, local instances,
notation, and simplification attributes. Inspect the extracted declaration and
check a consumer outside its defining namespace. Make important types explicit
where inference becomes expensive. Preserve mathematical intermediate names
when they explain the proof; do not inline a shared expression repeatedly to
save a binding.

Extract one boundary, refresh diagnostics, and check the consumer before moving
the next. If the extracted proof needs transported equalities everywhere,
revisit the representation or retain the step locally. A successful edit must
still establish the original theorem with the same trust assumptions.

## Large API replacement

Map old declarations to new ones and list consumer updates. Distinguish a rename
from an equivalent representation and from an intentionally changed statement.
For an equivalence, prove enough of the bridge that downstream users can recover
the public result; a prose claim of equivalence is insufficient.

Implement the smallest vertical slice from a real input to a public result.
Compare default simplification, extensionality, typeclass selection, and
elaboration cost. Only then propagate the design. If the original structure
encodes a useful invariant, flattening it may worsen safety or usability even
when it reduces projection syntax.

When removing modules, update the repository's documented generated-root tool
if it has one. Do not invent a `mk_all` target for a project that lacks it.
Check imports after deletion and ensure required declarations are still
reachable from published root targets. Keep experimental proofs, traces, and
temporary metadata outside the library tree.

For library extraction, review the intended statements and definitions first,
then introduce only the dependency closure required by accepted results. There
is no need to import the entire workspace or accept a frozen roadmap before
useful review begins. PR/issue discussion can refine the extraction while
the mathematical contract remains explicit.
