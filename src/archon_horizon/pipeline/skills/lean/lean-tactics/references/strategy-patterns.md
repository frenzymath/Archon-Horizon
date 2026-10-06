# Strategy Patterns

## Match the mathematical operation

| Goal or argument | Useful route | Check first |
| --- | --- | --- |
| Definitional equality | `rfl`, `change`, controlled unfolding | Actual definitions and reducibility |
| Function/morphism equality | `funext`, `ext`, established extensionality lemma | Correct bundled type and coercion |
| Polynomial identity | `ring`/`ring_nf` | Appropriate algebraic structure |
| Division identity | Nonzero side conditions then `field_simp` | Zero cases and totalized operations |
| Natural/integer linear constraints | `omega` | Nonlinear terms or truncated subtraction |
| Ordered-field linear consequences | `linarith` | Required inequalities present in context |
| Nonlinear inequality | Named bound, `nlinarith`, `positivity` | Products, squares, and sign facts |
| Monotone expression comparison | Monotonicity theorem, `gcongr`, `calc` | Direction and sign hypotheses |
| Topological/function property | Composition theorem, `continuity`, `fun_prop` | Actual domain, topology, local/global property |
| Almost-everywhere reasoning | Filter lemmas and `filter_upwards` | Correct filter/measure and measurable instances |

These are strategy candidates, not promises that one tactic solves a whole
domain. For quantifiers and implications, introduce named variables/hypotheses;
for conjunctions or witnesses, provide the intended components. Do not create
unknown witnesses and expect arithmetic automation to choose the mathematics.

## Prefer transfer over repeated analysis

A complicated function may agree with a simpler one on the relevant set or
neighborhood. Search `EqOn`, `EventuallyEq`, and `congr` APIs before duplicating
continuity, differentiation, or integration arguments by cases. Verify the
direction of equality required by the chosen theorem, and whether it needs
pointwise equality at the distinguished point in addition to eventual equality.

For finite sums, products, images, and cardinalities, search the corresponding
`Finset` API before induction with repeated insert/erase bookkeeping. For
morphisms, use the established extensionality API instead of manually unfolding
every structure field. For order arguments, compositional monotonicity and
supremum/infimum characterization lemmas often replace low-level case splits.

## Keep context under control

`rintro ... rfl` can eliminate a previously named variable rather than the
fresh variable you expected. If later proof steps need the outer name, introduce
the equality explicitly and rewrite or `subst` the intended variable by name.
Inspect the context after substitutions; stale names are not missing imports.

For class-typed local values, check which instance subsequent expressions use.
Pin the intended ambient structure when introducing another on the same carrier.
Use [definition-quality](../../../review/definition-quality/SKILL.md) if the
problem repeats across consumers.

`calc` follows the goal after the preceding tactics. Simplifying may change
division to inverse notation, norm to absolute value, or a coercion's shape.
Start from the displayed expression; do not add inverse rewrites solely to
force the proof back into an outdated draft. Choose one coherent normalization
before the calculation when that makes the argument readable.
