import Lean.Elab.Tactic.Omega

namespace VaterLocal

def maxUnsigned32 : Nat := 4294967295

def checkedAdd (left right : Nat) : Option Nat :=
  if left ≤ maxUnsigned32 ∧ right ≤ maxUnsigned32 ∧ left + right ≤ maxUnsigned32
  then some (left + right)
  else none

theorem checkedAdd_sound (left right result : Nat)
    (success : checkedAdd left right = some result) :
    result = left + right ∧ result ≤ maxUnsigned32 := by
  unfold checkedAdd at success
  split at success
  next allowed =>
    injection success with equal
    subst result
    exact ⟨rfl, allowed.2.2⟩
  next =>
    contradiction

theorem checkedAdd_complete (left right : Nat) (bounded : left + right ≤ maxUnsigned32) :
    checkedAdd left right = some (left + right) := by
  have leftBound : left ≤ maxUnsigned32 := by omega
  have rightBound : right ≤ maxUnsigned32 := by omega
  simp [checkedAdd, leftBound, rightBound, bounded]

theorem checkedAdd_overflow (left right : Nat) (overflow : maxUnsigned32 < left + right) :
    checkedAdd left right = none := by
  have notBounded : ¬ left + right ≤ maxUnsigned32 := by omega
  simp [checkedAdd, notBounded]

-- Only reserved work with a live grant and the necessary approval may dispatch.
def dispatchAllowed (prepared liveGrant needsApproval validApproval : Bool) : Bool :=
  prepared && liveGrant && (!needsApproval || validApproval)

theorem noDispatchWithoutApproval (prepared liveGrant : Bool) :
    dispatchAllowed prepared liveGrant true false = false := by
  cases prepared <;> cases liveGrant <;> decide

theorem noDispatchWithoutLiveGrant (prepared needsApproval validApproval : Bool) :
    dispatchAllowed prepared false needsApproval validApproval = false := by
  cases prepared <;> cases needsApproval <;> cases validApproval <;> decide

example : checkedAdd 0 4294967295 = some 4294967295 := by decide
example : checkedAdd 4294967295 1 = none := by decide
example : checkedAdd 4294967296 0 = none := by decide

#print axioms checkedAdd_sound
#print axioms checkedAdd_complete
#print axioms checkedAdd_overflow
#print axioms noDispatchWithoutApproval
#print axioms noDispatchWithoutLiveGrant

end VaterLocal
