namespace MiniTopology

/-! A tiny, dependency-free Lean surface for the dashboard demo. -/

def IsOpen (U : Set Nat) : Prop := ∀ n, n ∈ U → n ∈ U

theorem finite_compact (s : Finset Nat) : s.toSet.Finite := by
  exact s.finite_toSet

theorem singleton_is_open (n : Nat) : IsOpen ({n} : Set Nat) := by
  intro m hm
  exact hm

/- This is intentionally unfinished so the dashboard shows a partial frontier. -/
theorem discrete_connected_iff (s : Finset Nat) : s.card ≤ 1 := by
  sorry

end MiniTopology
