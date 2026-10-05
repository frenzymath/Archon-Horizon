# Rewrite Policy

## Normal form is relative to a configuration

The default simp set chooses a library-wide representation. A `simp only`
invocation has its own smaller rewrite policy. Active simprocs, congruence,
configuration, local hypotheses, and dischargers also affect the result.
Do not call an expression universally normal merely because it is mathematically
natural or short.

For a proposed global rule, simplify its left side with the surrounding policy
but without the rule itself. A left side already simplified by other lemmas may
never be reached. Check that the right side reaches the chosen destination;
syntactic size alone does not establish progress. The `simpNF` lint helps with
left-side redundancy, but does not certify the whole API's intended right-side
normal form.

Canonical rewrites typically remove administrative structure or select a stable
representation. Commutativity and ad hoc reassociation usually have no unique
preferred orientation. Keep such equalities local unless the API has a deliberate
normalization strategy. Check for competing rules rewriting the same pattern
and cycles recreating the left side through other lemmas.

## Locality choices

Use `rw` for an intended direction at a specific proof point; `simp only` for
a controlled normal form; a local simp attribute for a section-specific policy;
a named simp set for reusable optional normalization; and global `@[simp]` for
the stable default interface. Do not require broad API consumers to inherit a
specialized proof's preferred representation.

When a local proof fails after an attribute change, inspect the active rules
and result shape instead of compensating with more opposite rewrites. Test
representative clients of the old and new normal forms. A working local fix
does not establish that a global attribute is harmless.

## Computed rewriting

A simproc is justified when a computed rewrite cannot be expressed cleanly by
ordinary conditional lemmas and existing dischargers, or an infinite family of
explicit-data cases needs one procedure. Side conditions alone do not require
metaprogramming. Exhaust ordinary rules before adding a new runtime mechanism.

Use a definitionally equal computation (`dsimproc`) only when that equality is
the actual contract; use a proof-producing `simproc` when a witness is needed.
Match the pinned Lean version's API, because registration and result constructors
can change. Guard non-matching inputs cheaply and leave symbolic expressions
alone when no canonical computed result exists. Symbolic structural rewrites
are still valid when their canonical result and proof are well-defined.

Consider whether the procedure should run before or after children. A branch
selection can avoid expensive irrelevant traversal, but returning a result
that will not be revisited commits to its processing state. Prefer the simpler
correct traversal until measurements justify more control. Keep proof search
bounded and predictable; a simproc should not hide a general proof engine.

Test explicit inputs, symbolic nonmatches, termination, proof validity, and
representative combined simp sets. Benchmark against the lemma-only baseline
when performance is the reason for the feature. Remove tracing and speculative
global registrations from the published result.
