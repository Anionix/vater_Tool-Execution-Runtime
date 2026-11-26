"""Trusted, side-effect-free execution core for a local calculator tool.

Machine contract:
  AUTHORIZATION-001: A tool call never gets authority from caller arguments.
  INPUT-001: Only exact Python ints within the unsigned 32-bit range are accepted.
  ARITHMETIC-001: Addition returns an error instead of silently overflowing.
  STATE-001: All successful calls traverse the legal execution state machine.

The local authority is configured by the hosting process; this is not network
user authentication and must never be represented as one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from threading import Event
from time import monotonic_ns
from typing import Any, Mapping

LOCAL_PUBLISHER = "Anionix/vater_Tool-Execution-Runtime"
UNSIGNED_32_MAX = 2**32 - 1


class OperationError(Exception):
    """Expected error with a stable non-sensitive machine-readable code."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(detail)


class State(str, Enum):
    RECEIVED = "received"
    VALIDATED = "validated"
    AUTHORIZED = "authorized"
    RUNNING = "running"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


# STATE-001: model for proof / property-based testing / Rust port.
ALLOWED_TRANSITIONS: frozenset[tuple[State, State]] = frozenset({
    (State.RECEIVED, State.VALIDATED),
    (State.RECEIVED, State.REJECTED),
    (State.VALIDATED, State.AUTHORIZED),
    (State.VALIDATED, State.REJECTED),
    (State.AUTHORIZED, State.RUNNING),
    (State.RUNNING, State.COMPLETED),
    (State.RUNNING, State.FAILED),
})


class ExecutionTrace:
    def __init__(self) -> None:
        self.states: list[State] = [State.RECEIVED]

    def move(self, next_state: State) -> None:
        previous = self.states[-1]
        if (previous, next_state) not in ALLOWED_TRANSITIONS:
            raise OperationError("INVALID_TRANSITION", f"{previous.value} -> {next_state.value}")
        self.states.append(next_state)


@dataclass(frozen=True)
class LocalAuthority:
    """Server-configured local capability, never read from untrusted tool input."""
    allowed_tools: frozenset[str]
    allowed_destinations: frozenset[str] = field(default_factory=frozenset, kw_only=True)
    expires_at_monotonic_ns: int | None = field(default=None, kw_only=True)
    revocation_event: Event | None = field(default=None, kw_only=True, repr=False)

    def __post_init__(self) -> None:
        deadline = self.expires_at_monotonic_ns
        if deadline is not None and (type(deadline) is not int or deadline < 0):
            raise ValueError("invalid host grant deadline")

    def permits(self, tool_name: str, *, publisher: str = LOCAL_PUBLISHER,
                destination: str | None = None) -> bool:
        """task.trust.011: consult the live, process-local host grant."""
        return (
            publisher == LOCAL_PUBLISHER
            and tool_name in self.allowed_tools
            and (destination is None or destination in self.allowed_destinations)
            and (self.revocation_event is None or not self.revocation_event.is_set())
            and (self.expires_at_monotonic_ns is None or monotonic_ns() < self.expires_at_monotonic_ns)
        )

    @classmethod
    def calculator_only(cls) -> "LocalAuthority":
        return cls(frozenset({"checked_add"}))


@dataclass(frozen=True)
class ExecutionResult:
    result: int | None
    error_code: str | None
    states: tuple[str, ...]

    @property
    def succeeded(self) -> bool:
        return self.error_code is None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.succeeded, "value": self.result, "error_code": self.error_code,
                "states": list(self.states)}


def require_unsigned_32(value: Any, label: str) -> int:
    # Exact type check rejects bool (bool subclasses int in Python).
    if type(value) is not int:
        raise OperationError("INVALID_ARGUMENT", f"{label} must be an unsigned 32-bit integer")
    if not 0 <= value <= UNSIGNED_32_MAX:
        raise OperationError("OUT_OF_RANGE", f"{label} out of unsigned 32-bit range")
    return value


def validated_arguments(arguments: Any) -> tuple[int, int]:
    if not isinstance(arguments, dict):
        raise OperationError("INVALID_ARGUMENT", "arguments must be an object")
    if set(arguments) != {"left", "right"}:
        raise OperationError("INVALID_ARGUMENT", "arguments require only left and right")
    return require_unsigned_32(arguments["left"], "left"), require_unsigned_32(arguments["right"], "right")


def checked_add(left: int, right: int) -> int:
    # Inputs must have passed validated_arguments; enforce again at this boundary.
    left = require_unsigned_32(left, "left")
    right = require_unsigned_32(right, "right")
    result = left + right
    if result > UNSIGNED_32_MAX:
        raise OperationError("OVERFLOW", "32-bit unsigned addition overflow")
    return result


def __getattr__(name: str) -> Any:
    # CalculationService is the historical name of the protocol-independent
    # runtime (runtime.ToolRuntime). Resolved lazily: runtime imports this module.
    if name == "CalculationService":
        from .runtime import ToolRuntime
        return ToolRuntime
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
