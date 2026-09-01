import Mathlib

abbrev putnam_2020_a2_solution : ℕ → ℕ := by
  sorry

theorem putnam_2020_a2
  (k : ℕ) :
  (∑ j ∈ Finset.Icc 0 k, 2 ^ (k - j) * Nat.choose (k + j) j) =
    putnam_2020_a2_solution k := by
  sorry
