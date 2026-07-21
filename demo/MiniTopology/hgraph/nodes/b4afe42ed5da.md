---
author: sync
content_type: theorem
created: '2026-07-20T16:02:10'
decl: MiniTopology.finite_compact
file: MiniTopology.lean
generated: lean
lean_status: lean_ok
title: MiniTopology.finite_compact
type: lean
updated: '2026-07-20T16:02:10'
---
theorem finite_compact (s : Finset Nat) : s.toSet.Finite := by
  exact s.finite_toSet