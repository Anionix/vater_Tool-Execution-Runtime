//! Kani harnesses (task.formal_verification.011 / 012 / 013). Each compares the port with a
//! specification written separately here, for every input Kani can choose. Every assumption
//! is listed and justified in docs/kani_assumptions.json, and every harness has cover
//! statements that Kani must report as satisfied, so no assumption can empty the harness.
use crate::*;

/// The sum in 128-bit arithmetic, stated without the port's comparisons.
fn add_specification(left: u64, right: u64) -> Result<u64, AddError> {
    let bound: u128 = 1u128 << 32;
    let (wide_left, wide_right) = (left as u128, right as u128);
    if wide_left >= bound || wide_right >= bound {
        Err(AddError::OutOfRange)
    } else if wide_left + wide_right >= bound {
        Err(AddError::Overflow)
    } else {
        Ok((wide_left + wide_right) as u64)
    }
}

#[kani::proof]
fn checked_add_matches_the_specification_for_every_u64_pair() {
    let left: u64 = kani::any();
    let right: u64 = kani::any();
    let result = checked_add(left, right);
    assert_eq!(result, add_specification(left, right));
    if let Ok(sum) = result {
        assert!(sum <= UNSIGNED_32_MAX && sum >= left && sum >= right);
    }
    kani::cover!(result.is_ok(), "a sum within range");
    kani::cover!(result == Err(AddError::Overflow), "an overflow");
    kani::cover!(result == Err(AddError::OutOfRange), "an input out of range");
}

#[kani::proof]
fn every_unsigned_32_pair_adds_exactly_or_overflows() {
    let left: u32 = kani::any();
    let right: u32 = kani::any();
    let mathematical = left as u64 + right as u64;
    match checked_add(left as u64, right as u64) {
        Ok(sum) => assert_eq!(sum, mathematical),
        Err(error) => {
            assert_eq!(error, AddError::Overflow);
            assert!(mathematical > UNSIGNED_32_MAX);
        }
    }
    kani::cover!(left == u32::MAX && right == 0, "the largest sum that fits");
    kani::cover!(left == u32::MAX && right == 1, "the smallest overflow");
}

/// The seven allowed transitions, listed independently of the port's match expression.
const ALLOWED: [(State, State); 7] = [
    (State::Received, State::Validated),
    (State::Received, State::Rejected),
    (State::Validated, State::Authorized),
    (State::Validated, State::Rejected),
    (State::Authorized, State::Running),
    (State::Running, State::Completed),
    (State::Running, State::Failed),
];

#[kani::proof]
#[kani::unwind(8)]
fn a_transition_is_allowed_exactly_when_it_is_listed() {
    let from_index: u8 = kani::any();
    let to_index: u8 = kani::any();
    kani::assume(from_index < 7 && to_index < 7);
    let from = State::from_index(from_index).unwrap();
    let to = State::from_index(to_index).unwrap();
    let listed = ALLOWED.iter().any(|&(a, b)| a == from && b == to);
    assert_eq!(transition_allowed(from, to), listed);
    kani::cover!(listed, "an allowed transition");
    kani::cover!(!listed, "a forbidden transition");
}

#[kani::proof]
#[kani::unwind(8)]
fn exactly_seven_of_the_forty_nine_transitions_are_allowed() {
    let mut allowed = 0;
    for from in State::ALL {
        for to in State::ALL {
            if transition_allowed(from, to) {
                allowed += 1;
            }
        }
    }
    assert_eq!(allowed, 7);
    for state in State::ALL {
        assert!(!transition_allowed(state, state));
    }
    for terminal in [State::Completed, State::Rejected, State::Failed] {
        for to in State::ALL {
            assert!(!transition_allowed(terminal, to));
        }
    }
    kani::cover!(allowed == 7, "all forty-nine pairs were examined");
}

/// The grant policy written as a sequence of refusals.
fn permits_specification(grant: &Grant, local: bool, tool: u8, destination: Option<u8>, now: u64) -> bool {
    if !local || tool >= 64 || (grant.allowed_tools >> tool) & 1 == 0 {
        return false;
    }
    if let Some(d) = destination {
        if d >= 64 || (grant.allowed_destinations >> d) & 1 == 0 {
            return false;
        }
    }
    if grant.revoked {
        return false;
    }
    match grant.expires_at_ns {
        Some(deadline) if now >= deadline => false,
        _ => true,
    }
}

#[kani::proof]
fn permits_is_the_grant_policy_for_every_grant_and_request() {
    let grant = Grant {
        allowed_tools: kani::any(),
        allowed_destinations: kani::any(),
        revoked: kani::any(),
        expires_at_ns: kani::any(),
    };
    let local: bool = kani::any();
    let tool: u8 = kani::any();
    let destination: Option<u8> = kani::any();
    let now: u64 = kani::any();
    let decision = permits(&grant, local, tool, destination, now);
    assert_eq!(decision, permits_specification(&grant, local, tool, destination, now));
    if !local || grant.revoked {
        assert!(!decision);
    }
    if let Some(deadline) = grant.expires_at_ns {
        if now >= deadline {
            assert!(!decision);
        }
    }
    kani::cover!(decision, "a permitted request");
    kani::cover!(!decision && local && !grant.revoked && grant.expires_at_ns.is_none(), "refused by tool or destination");
    kani::cover!(
        !decision && local && !grant.revoked && destination.is_none() && tool < 64 && (grant.allowed_tools >> (tool % 64)) & 1 == 1,
        "refused only by the deadline"
    );
}

fn any_binding() -> Binding {
    Binding {
        principal: kani::any(),
        tool: kani::any(),
        target: kani::any(),
        resource_version: kani::any(),
        arguments_digest: kani::any(),
    }
}

#[kani::proof]
fn an_approval_admits_only_its_exact_unused_unexpired_binding() {
    let operation = any_binding();
    let approval = Approval { binding: any_binding(), expires_at_ms: kani::any(), consumed: kani::any() };
    let present: bool = kani::any();
    let now: u64 = kani::any();
    let admitted = approval_admits(&operation, if present { Some(&approval) } else { None }, now);
    let fields_equal = approval.binding.principal == operation.principal
        && approval.binding.tool == operation.tool
        && approval.binding.target == operation.target
        && approval.binding.resource_version == operation.resource_version
        && approval.binding.arguments_digest == operation.arguments_digest;
    assert_eq!(admitted, present && !approval.consumed && now < approval.expires_at_ms && fields_equal);
    kani::cover!(admitted, "an admitted approval");
    kani::cover!(present && fields_equal && !admitted, "refused for use or expiry");
    kani::cover!(present && !approval.consumed && now < approval.expires_at_ms && !fields_equal, "refused for a different binding");
}
