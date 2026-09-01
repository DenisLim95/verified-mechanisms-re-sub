import Mathlib

/-- The largest natural `n` with `n ! < 3 ^ n`. Must be a numeric literal. -/
abbrev p10_answer : ℕ := sorry

/-- `p10_answer` is the greatest element of `{n : ℕ | n ! < 3 ^ n}`. -/
theorem p10_factorial_pow :
    IsGreatest {n : ℕ | Nat.factorial n < 3 ^ n} p10_answer := by
  sorry
