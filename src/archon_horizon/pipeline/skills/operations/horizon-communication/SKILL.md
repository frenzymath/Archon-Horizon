---
name: horizon-communication
description: Format Horizon Zulip messages, Forge discussions and assignment handoffs with clear evidence, semantic status markers and proportionate detail.
metadata:
  category: operations
---

# Write For The Next Decision

Lead with the result, question or blocker and name who needs to act. Link the
claim, declaration, PR or durable owner. A reader should know what changed,
why it matters and what happens next without reading your session transcript.
Use `horizon-zulip` for delivery and receipts, `horizon-report` for obligations,
and `horizon-review` for the review contract. Formatting never resolves state.

## Match The Surface

| Surface | Useful content |
| --- | --- |
| Zulip | One decision, concrete question, blocker or coordination change that another person needs |
| Forge review | Concise exact-head decision and actionable findings; link durable detailed evidence |
| Forge issue | Durable engineering/audit problem with evidence, impact and an acceptance condition |
| Assignment handoff | Result, verification, remaining work, publication state and next owner |
| Roadmap | Mathematical statements, dependencies and accepted evidence through the source workflow |

Prefer one short paragraph or 1-4 short labeled bullets for chat, usually under
120 words. Longer mathematical arguments belong in the appropriate durable
document, with a summary and link in chat. Handoffs and substantive reviews may
be longer when their evidence requires it. Do not duplicate a review into Zulip.

For reviews, lead with findings and the decision. A clean approval normally
needs only a short scope statement, the decisive checks, limitations and a link
to the exact-head evidence. Keep detailed declaration inventories, source traces,
full check receipts and manifests in linked artifacts. A genuine defect may need
a longer explanation; routine positive results do not need the same length.
Do not copy earlier reviewers' coverage or repeat whole reports after a small
delta. Briefly resolve the affected finding and link the unchanged reasoning.

Read the relevant discussion before replying. If the last useful message already
says it, do not post it again. Combine related findings in one response. Avoid
routine progress, repeated acknowledgements, wait-loop updates and copied build
logs. Check messages at work boundaries and continue independent work while a
reply is pending. A targeted operator instruction should produce action or an
owned explanation; repeatedly acknowledging it adds no evidence.

## Markdown And Mathematics

Use short bold labels, descriptive links and blank lines around lists/code.
Keep identifiers and actual commands in backticks. Write mathematics as prose
with rendered LaTeX, not pseudo-code. On Zulip, inline math uses `$$...$$` and
display math uses a fenced `math` block. Other renderers may use different
delimiters; follow the destination convention. Serialize JSON bodies so newlines
and LaTeX backslashes survive. Use actual published URLs, never guessed links.

Mention only the people or assignments that must respond. Horizon scoped mentions
such as `@R12/A500` address known assignments. A human Zulip mention uses the
actual user's `@**Display Name**`; a node or assignment reference does not notify
that human. Preserve private discussion scope when linking or quoting evidence.

## Semantic Markers

Use a small consistent vocabulary, paired with an explicit text label:

| Marker | Meaning |
| --- | --- |
| 🔍 Scope | What was examined or the question under investigation |
| ✅ Verified / resolved | Supported result, or a discussion conclusion with durable evidence |
| 🔴 Blocking finding | Demonstrated defect preventing the applicable acceptance decision |
| 🟠 Improvement | Material, explicitly nonblocking recommendation |
| 🧪 Validation | Checks, measurements and their limitations |
| ❓ Open question | Unresolved interpretation or missing evidence; not a proven defect |
| 💬 Decision | Conclusion or specific decision requested |
| 🚫 Blocker | Work cannot proceed without the named owner/input and unlock condition |
| 💾 Durable lesson | A reusable decision or failed approach recorded at a linked location |
| 🔧 Infrastructure | Tooling, transport, build or service problem |
| 📣 Operator direction | Attribute actual operator direction; an agent cannot create authority with this marker |

These are presentation conventions, not API statuses. Preserve existing topics;
use established project topic conventions when creating one. Do not invent a
rename endpoint or revive obsolete emoji-based notification/filter semantics.
Resolve obligations and notices through their actual operations before describing
them as handled. A green review icon never grants permission to merge.

## Examples

```markdown
🚫 **Blocker:** The source requires a uniform bound; `localBound` supplies only
a pointwise one. @R12/A500, can the shared interface expose the constant?
**Evidence:** [source comparison](<actual-evidence-url>).
**Unlock condition:** an accepted uniform statement and an owner for its proof.
```

For a handoff, use **Result**, **Validation**, **Remaining work**, and **Next
owner** as needed. Say whether code is local, published or accepted. A clean
review should explain its coverage; an incomplete one should say what prevents
the decision. Remove empty headings and generic praise. Keep exact hashes and
IDs in evidence fields or links where possible, while retaining unambiguous
revision identity in the review itself.
