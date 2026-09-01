import Mathlib

open Nat

theorem rmo_2001_2 (p q : ℕ) (hp : Nat.Prime p) (hq : Nat.Prime q) :
  (∃ m : ℕ, p^2 + 7*p*q + q^2 = m^2) ↔
    (p = q ∨ (p = 3 ∧ q = 11) ∨ (p = 11 ∧ q = 3)) := by
  sorry
