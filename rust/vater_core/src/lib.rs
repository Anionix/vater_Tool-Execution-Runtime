//! task.formal_verification.011 / 012: a Rust port of the pure decision functions of the Python
//! core. Kani verifies them for every input (src/proofs.rs), and tests/expected_values.rs checks
//! them against the same literal expected values the Python tests use
//! (tests/fixtures/independent_expected_values.txt, task.formal_verification.015).
//!
//! Ported (Python source in parentheses):
//!   checked_add         (core.require_unsigned_32 and core.checked_add)
//!   transition_allowed  (core.ALLOWED_TRANSITIONS)
//!   permits             (core.LocalAuthority.permits, with the clock as an input)
//!   approval_admits     (the approval check of ledger.OperationLedger.begin_dispatch)
//! Not ported: persistence, SQLite transactions, threads, and the MCP and HTTP adapters.
#![forbid(unsafe_code)]

/// The largest unsigned 32-bit value.
pub const UNSIGNED_32_MAX: u64 = 4_294_967_295;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AddError {
    /// An input is not an unsigned 32-bit value (Python: OUT_OF_RANGE).
    OutOfRange,
    /// The sum exceeds the unsigned 32-bit range (Python: OVERFLOW).
    Overflow,
}

/// Python integers are unbounded; this port takes u64, which covers every 32-bit input and
/// the out-of-range values up to 2^64 - 1. Larger values are outside the port's domain.
pub fn checked_add(left: u64, right: u64) -> Result<u64, AddError> {
    if left > UNSIGNED_32_MAX || right > UNSIGNED_32_MAX {
        return Err(AddError::OutOfRange);
    }
    let sum = left + right; // both are at most 2^32 - 1, so this cannot overflow u64
    if sum > UNSIGNED_32_MAX {
        Err(AddError::Overflow)
    } else {
        Ok(sum)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Received,
    Validated,
    Authorized,
    Running,
    Completed,
    Rejected,
    Failed,
}

impl State {
    pub const ALL: [State; 7] = [
        State::Received,
        State::Validated,
        State::Authorized,
        State::Running,
        State::Completed,
        State::Rejected,
        State::Failed,
    ];

    pub fn from_index(index: u8) -> Option<State> {
        Self::ALL.get(index as usize).copied()
    }

    pub fn from_name(name: &str) -> Option<State> {
        Self::ALL.iter().copied().find(|state| state.name() == name)
    }

    pub fn name(self) -> &'static str {
        match self {
            State::Received => "received",
            State::Validated => "validated",
            State::Authorized => "authorized",
            State::Running => "running",
            State::Completed => "completed",
            State::Rejected => "rejected",
            State::Failed => "failed",
        }
    }
}

/// The read-only computation's state machine: seven allowed transitions, all others forbidden.
pub fn transition_allowed(from: State, to: State) -> bool {
    use State::*;
    matches!(
        (from, to),
        (Received, Validated)
            | (Received, Rejected)
            | (Validated, Authorized)
            | (Validated, Rejected)
            | (Authorized, Running)
            | (Running, Completed)
            | (Running, Failed)
    )
}

/// A host grant. Tools and destinations are indices (below 64) into the host's registries.
#[derive(Clone, Copy, Debug)]
pub struct Grant {
    pub allowed_tools: u64,
    pub allowed_destinations: u64,
    pub revoked: bool,
    pub expires_at_ns: Option<u64>,
}

/// LocalAuthority.permits with the clock as an input: the local publisher, a granted tool,
/// a granted destination when one is named, not revoked, and before the deadline.
pub fn permits(grant: &Grant, local_publisher: bool, tool: u8, destination: Option<u8>, now_ns: u64) -> bool {
    local_publisher
        && tool < 64
        && grant.allowed_tools & (1u64 << tool) != 0
        && destination.map_or(true, |d| d < 64 && grant.allowed_destinations & (1u64 << d) != 0)
        && !grant.revoked
        && grant.expires_at_ns.map_or(true, |deadline| now_ns < deadline)
}

/// What an approval and a reservation are bound to (identifiers stand for the strings and the
/// canonical argument digest the ledger compares).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Binding {
    pub principal: u32,
    pub tool: u32,
    pub target: u32,
    pub resource_version: u32,
    pub arguments_digest: u64,
}

#[derive(Clone, Copy, Debug)]
pub struct Approval {
    pub binding: Binding,
    pub expires_at_ms: u64,
    pub consumed: bool,
}

/// begin_dispatch consumes an approval only if it exists, is unused, has not expired at the
/// ledger's clock, and binds exactly the reserved operation; otherwise APPROVAL_INVALID.
pub fn approval_admits(operation: &Binding, approval: Option<&Approval>, now_ms: u64) -> bool {
    match approval {
        None => false,
        Some(found) => !found.consumed && now_ms < found.expires_at_ms && found.binding == *operation,
    }
}

#[cfg(kani)]
mod proofs;
