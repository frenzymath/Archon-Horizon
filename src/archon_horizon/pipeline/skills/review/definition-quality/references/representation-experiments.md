# Representation Experiments

## Compare public contracts

Record which facts are definitional, which are propositional equalities, and
which need an equivalence or coercion. A newtype can prevent instance conflicts
and support genuinely distinct structures. An alias may preserve convenient
definitional equality. Neither should be chosen by a blanket rule.

A bundled morphism should reuse the library's composition, coercion,
extensionality, and identity API when it fits. Before introducing a bespoke
map structure, try expressing the actual consumer with an existing `LinearMap`,
continuous map, homomorphism, or equivalence. If the existing abstraction is
too strong, state precisely which unwanted field or assumption it imposes.

For predicates, distinguish a property of an existing object from a structure
carrying chosen data. Turning existence into chosen data may introduce choice
and make equality or transport expensive. Conversely, forcing repeated
existential extraction may hide data the consumers always need together.
Compare both on the actual constructor and a downstream use.

## Instance selection

Different structures on the same carrier can make two visually identical
types differ. Newly introduced class-typed local values can change subsequent
instance synthesis. Previously elaborated values do not retroactively change.
Inspect explicit arguments with `#print`, `@declaration`, and available LSP
hover instead of repeatedly rewriting the apparent type.

Name an important ambient instance in the binder when designing the API.
When a signature is fixed, capture the ambient instance before introducing an
alternative, then use explicit instance arguments on facts that must refer to
it. A `let` or `set` naming a competing structure is not automatically a fix;
the issue is which instance later expressions select.

For measure theory, a pulled-back measurable space on the domain of `f` is
`MeasurableSpace.comap f` applied to the *codomain's* measurable space. It is
not the ambient structure on the domain. Keep ambient measurability facts
distinct from facts about the pulled-back structure. Similar care applies to
alternate metrics, orders, and algebraic structures.

Do not export a global instance merely to repair one proof. Test local
instances first and inspect priority, synthesis loops, and interactions with
existing instances. Use a type synonym when two structures genuinely need
different identities throughout an API, with explicit conversions.

## Small experiments

Keep the original public theorem fixed. Compare a representative constructor,
an operation, a simplification, and one theorem under the old and candidate
representation. Record explicit transports and elaboration diagnostics.
Measure import and consumer cost with the same toolchain and cache state.

If one bridge theorem makes all consumers ordinary, prefer that repair over a
large rewrite. If every consumer rebuilds the same context or climbs nested
wrappers, try flattening the API. Remove obsolete constructors/instances only
after searching callers, root imports, documentation, and graph references.

Adapted instance-selection guidance from Cameron Freer's `instance-pollution.md`;
see [provenance](../../../_sources/lean4-skills/PROVENANCE.md).
