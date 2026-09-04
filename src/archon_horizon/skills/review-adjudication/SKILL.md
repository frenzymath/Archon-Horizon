---
name: review-adjudication
description: Reproduce a disputed or high-severity review finding and classify it as confirmed, false alarm, overclaim, superseded, or unresolved without launching a second broad audit.
---

# Review adjudication

Use this focused lane when independent reports disagree, a proposed blocker
looks tactic- or environment-shaped, or a repair claims to close a prior issue.
Start from the exact prior finding and current revision.

## Reproduce

- Read the prior report only after recording its claim, location, snapshot, and
  requested evidence.
- Re-run the smallest command, deletion/minimality probe, source comparison,
  consumer check, or declaration inspection that could distinguish the claims.
- Check whether the code, source, or environment changed enough to supersede
  the original finding; preserve the original scope and non-goals.
- Keep a false alarm, an overclaim, and an unresolved boundary distinct from a
  confirmed defect. Do not silently average contradictory reports.

This is not a general review and not a repair job. Pair with the lane that owns
the disputed question, [[review-method]], and [[verification-evidence]] when the
dispute is about tooling or artifacts.

## Report

State prior/current snapshots, disputed claim, reproduction probe and output,
disposition (`confirmed`, `false-alarm`, `overclaim`, `superseded`, or
`unresolved`), residual risk, owner, and confidence. Remain read-only and never
mark the task complete.
