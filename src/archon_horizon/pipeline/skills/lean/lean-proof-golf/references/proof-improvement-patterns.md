# Proof Improvement Patterns

## Change the route before changing the syntax

Read the theorem's actual mathematical content and search its conclusion in
the pinned library. A general theorem with a simple specialization can replace
an entire bespoke development. Verify hypotheses and coercions in a small
consumer. Do not leave unused infrastructure behind after replacing its only
consumer; search before deleting it.

Look for duplicated symmetric arguments, repeated conversions, or witness
constructions that already have a standard lemma. Extract a named fact where
there is mathematical reuse, not merely to move lines out of sight. An API
change belongs to `lean-refactor`, not an unnoticed proof-golf edit.

## Small checked transformations

These are candidates, not unconditional rewrite rules:

| Existing shape | Candidate | Preserve when |
| --- | --- | --- |
| `by exact term` | `term` | Tactic mode is needed for local context/inference |
| `apply lemma; exact h` | `exact lemma h` | Explicit subgoals improve clarity or inference |
| `fun x => f x` | `f` | Expected types or dependent arguments need the lambda |
| `have h := shortTerm; exact use h` | `use shortTerm` | The name explains meaning or controls elaboration |
| Short `calc` with two routine links | `.trans`/`.symm` composition | The calculation explains the mathematical argument |
| `ext x; rfl` | `rfl` | Equality is propositional rather than definitional |
| Broad rewrite search | Direct `.mp`/`.mpr` application | The goal genuinely needs normalization |

Check in the real file context. Lean elaboration is sensitive to expected types
and metavariable order; a syntactically shorter term may fail or take longer.
Repeated local expressions may benefit from a named `let`, even when inlining
reduces line count. Keep types where they constrain expensive inference.

## Simplification and solvers

`simp?` can expose the used lemma set. Narrowing nonterminal normalization can
make later proof steps predictable, but terminal `simp` is often an intentional
stable library idiom. Follow local conventions and measured need; do not replace
every terminal `simp` with a long brittle list for aesthetic consistency.

Replace exploratory tactics such as `exact?` or `apply?` with their useful
suggested proof when it improves predictability. A stable domain solver can
remain. Solver selection is not a universal speed ladder: compare actual
elaboration when performance motivates the edit.

Remove unused simp arguments or dead facts only after the compiler/linter
confirms the final form. Global simplification attributes affect unrelated
consumers and need broader testing than a local proof change.

## Explain the result

Describe a shorter route, removed duplication, clearer dependency, or measured
cost change. Avoid invented percentages or generic claims that fewer lines are
more robust. Preserve statement fidelity, axiom assumptions, required imports,
and useful documentation. A no-change conclusion is appropriate for an already
clear proof.
