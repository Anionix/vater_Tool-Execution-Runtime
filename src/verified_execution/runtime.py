"""Single protocol-independent entry point for executing registered tools.

Every adapter calls ToolRuntime instead of implementing its own checks:

  execute(name, arguments)          validate -> authorize -> run -> verify
  release(name, arguments, result)  re-check the live grant and the result
                                    at the moment an adapter hands it over
  check(name, arguments)            validation only, for adapters that must
                                    reject a request before queueing it

Machine contracts:
  AUTHORIZATION-001: authority comes only from the host-supplied LocalAuthority.
  STATE-001: every call follows core.ALLOWED_TRANSITIONS.
  RISK-001: this runtime executes only read_only tools. State-changing,
            irreversible and code-executing tools need the durable ledger
            path and are rejected here with UNSUPPORTED_OPERATION.
  OUTPUT-001: no result leaves execute() or release() without verify_result.
"""
from __future__ import annotations

from typing import Any

from .core import ExecutionResult, ExecutionTrace, LocalAuthority, OperationError, State
from .tools import ToolRegistry, ToolSpec, default_registry

EXECUTABLE_RISKS = frozenset({"read_only"})


def _states(trace: ExecutionTrace) -> tuple[str, ...]:
    return tuple(state.value for state in trace.states)


class ToolRuntime:
    def __init__(self, authority: LocalAuthority, registry: ToolRegistry | None = None) -> None:
        self.authority = authority
        self.registry = registry if registry is not None else default_registry()

    def tools(self) -> tuple[ToolSpec, ...]:
        """Specs this runtime can execute, for adapters to advertise."""
        return tuple(spec for spec in self.registry if spec.risk in EXECUTABLE_RISKS)

    def permits(self, name: str) -> bool:
        return self.authority.permits(name)

    def _executable(self, name: object) -> ToolSpec:
        spec = self.registry.get(name)
        if spec.risk not in EXECUTABLE_RISKS:
            raise OperationError("UNSUPPORTED_OPERATION", "tool requires the durable operation path")
        return spec

    def check(self, name: object, arguments: Any) -> Any:
        """Return trusted arguments or raise OperationError. Does not authorize."""
        return self._executable(name).validate(arguments)

    def execute(self, name: str, arguments: Any) -> ExecutionResult:
        trace = ExecutionTrace()
        try:
            spec = self._executable(name)
            trusted = spec.validate(arguments)
        except OperationError as exc:
            trace.move(State.REJECTED)
            return ExecutionResult(None, exc.code, _states(trace))

        trace.move(State.VALIDATED)
        # AUTHORIZATION-001: only the server-supplied authority is consulted.
        if not self.authority.permits(spec.name):
            trace.move(State.REJECTED)
            return ExecutionResult(None, "NOT_AUTHORIZED", _states(trace))

        trace.move(State.AUTHORIZED)
        trace.move(State.RUNNING)
        try:
            value = spec.run(trusted)
            spec.verify_result(trusted, value)
        except OperationError as exc:
            trace.move(State.FAILED)
            return ExecutionResult(None, exc.code, _states(trace))
        except Exception:
            # A tool bug must not leak details through any adapter.
            trace.move(State.FAILED)
            return ExecutionResult(None, "EXECUTION_FAILED", _states(trace))
        trace.move(State.COMPLETED)
        return ExecutionResult(value, None, _states(trace))

    def release(self, name: str, arguments: Any, result: ExecutionResult) -> ExecutionResult:
        """Final check immediately before an adapter sends a result.

        task.trust.007: the value is re-verified against the original arguments.
        task.trust.011: a grant withdrawn after execution suppresses the value.
        """
        if not result.succeeded:
            return result
        if not self.authority.permits(name):
            return ExecutionResult(None, "NOT_AUTHORIZED", result.states)
        try:
            spec = self._executable(name)
            spec.verify_result(spec.validate(arguments), result.result)
        except OperationError:
            return ExecutionResult(None, "INVALID_TOOL_RESULT", result.states)
        return result
