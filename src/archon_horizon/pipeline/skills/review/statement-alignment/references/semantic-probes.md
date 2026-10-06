# Semantic Probes

## Quantifiers and scope

Write quantifiers with dependencies before comparing prose. A uniform constant
`exists C, forall x, bound C x` is stronger than a pointwise constant
`forall x, exists C, bound C x`. Conversely, proving one finite instance does
not establish a universally quantified family. Track which choices depend on
the point, parameter, cover, approximation, or error tolerance.

Expand section variables and typeclass hypotheses with `#check`/`#print` or
hover on the actual declaration. Lean includes only parameters needed by the
declaration; the surrounding file's informal setting is not its type. Check
that a crucial hypothesis was not dropped, or that a strong instance was not
introduced implicitly. Compare open/closed endpoints, strict/non-strict
inequalities, finite/infinite index sets, and local/global existence.

## Definitions and apparent shortcuts

Trace source-facing predicates through aliases and structures. Ask what data
the caller must supply and what the result actually constructs. For example,
`theorem result (h : P) : P := h` is valid but contributes no producer for `P`.
A structure with a field `conclusion : P` is also a conditional interface;
renaming it `Certificate` does not establish the intended theorem. A useful
certificate has a construction from the original hypotheses and a proved
consumer reaching the public result.

An empty structure is not itself unsound. It is wrong only when the advertised
mathematical content requires data or properties the structure does not carry.
Similarly, `True`, `False`, empty sets, and `Subsingleton` can be legitimate.
Determine whether they trivialize this claim contrary to its source, rather
than rejecting these tokens mechanically.

Read definitions for finite proxies, chosen coordinates, quotient relations,
regularity and topology. A coordinate computation can be correct while the
bridge to the original geometric theorem is missing. An existence theorem for
a wrapper may only restate an assumed existence theorem. Identify the exact
unproved implication instead of calling the whole proof invalid.

## Separating examples

Choose a tiny case where competing interpretations differ. Useful probes
include empty and singleton domains, zero and unit values, endpoints, a
non-injective map, a non-complete space, or a degenerate dimension. Keep the
probe inside every stated hypothesis. A counterexample outside the domain
does not refute the theorem.

Totalized operations are a common source of mismatch: division and inverse at
zero, subtraction on naturals, extended real arithmetic, and suprema or
infima of empty sets. Inspect the pinned library's convention instead of
assuming the paper's partial-operation convention. Check whether the intended
nondegeneracy condition appears at the actual use site.

Test whether a suspicious dependency contributes to the proof by temporarily
removing or replacing its use in an isolated probe. Success may show dead
infrastructure, but is not itself a defect: redundant hypotheses or alternate
proofs can be harmless. The finding needs a consequence for the intended claim,
reusability, or maintainability.

## Evidence and disposition

Present the exact mismatch as two statements and a witness or missing bridge.
For a repaired theorem, recompare the changed binders and definitions. Do not
silently strengthen assumptions to make the old proof compile. If the source
itself is ambiguous or false, preserve the evidence and arrange an owned
mathematical decision through the assignment's ledger and project discussion.
Independent reviewers advise; the repository policy decides merge authority.
