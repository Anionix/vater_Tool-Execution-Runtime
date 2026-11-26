"""Single protocol-independent entry point for executing registered tools.

Every adapter calls ToolRuntime instead of implementing its own checks:

  acall(name, arguments)            the one async entry point for adapters:
                                    read-only tools run under the host's
                                    time and concurrency budget; durable
                                    tools run through the host-attached
                                    ledger executor. Both are verified.
  execute(name, arguments)          synchronous read-only path:
                                    validate -> authorize -> run -> verify
  release(name, arguments, result)  re-check the live grant and the result
                                    at the moment an adapter hands it over
  check(name, arguments)            validation only, for adapters that must
                                    reject a request before queueing it

Machine contracts:
  AUTHORIZATION-001: authority comes only from the host-supplied LocalAuthority,
                     consulted under each spec's grant name.
  STATE-001: every read-only call follows core.ALLOWED_TRANSITIONS.
  RISK-001: read_only tools run in-process. state_change tools run only
            through a host-attached durable executor (ledger reservation,
            dispatch, commit, replay). irreversible and code_execution tools
            are refused: they need an approval flow that is not wired here.
  OUTPUT-001: no result leaves acall(), execute() or release() unverified.
"""
from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import TYPE_CHECKING, Any, Mapping, Protocol

from .core import ExecutionResult, ExecutionTrace, LocalAuthority, OperationError, State
from .tools import ToolRegistry, ToolSpec, default_registry

if TYPE_CHECKING:
    from .request_limits import ReadOnlyPolicy

# The synchronous path (execute/release/check) needs only the standard
# library, so the core stays importable without third-party packages.
# anyio is imported lazily by the async entry point.

EXECUTABLE_RISKS = frozenset({"read_only"})
DURABLE_RISKS = frozenset({"state_change"})


def _states(trace: ExecutionTrace) -> tuple[str, ...]:
    return tuple(state.value for state in trace.states)


class DurableExecutor(Protocol):
    """A ledger bridge: `acall(name, arguments)` (async) or `call(name, arguments)`.

    Returns an object with ok, value, operation_id, replayed and error_code,
    such as recorded_tool_bridge.BridgeResult.
    """


@dataclass(frozen=True)
class ToolOutcome:
    """Protocol-independent outcome of acall(), already verified for release."""
    ok: bool
    value: Any
    error_code: str | None
    operation_id: str | None = None
    replayed: bool | None = None

    @property
    def durable(self) -> bool:
        return self.operation_id is not None

    def structured(self) -> dict[str, Any]:
        """Success payload that matches the spec's output schema."""
        payload: dict[str, Any] = {"result": self.value}
        if self.durable:
            payload.update(operation_id=self.operation_id, replayed=self.replayed)
        return payload


def _failure(code: str | None) -> ToolOutcome:
    return ToolOutcome(False, None, code or "EXECUTION_FAILED")


class ToolRuntime:
    def __init__(self, authority: LocalAuthority, registry: ToolRegistry | None = None, *,
                 durable: Mapping[str, DurableExecutor] | None = None,
                 readonly_policy: ReadOnlyPolicy | None = None) -> None:
        self.authority = authority
        self.registry = registry if registry is not None else default_registry()
        bindings = dict(durable or {})
        for name in bindings:
            spec = self.registry.get(name)
            if spec.risk not in DURABLE_RISKS:
                raise ValueError(f"durable executor bound to a {spec.risk} tool: {name}")
        self._durable = bindings
        self._bounded = None
        if readonly_policy is not None:
            from .request_limits import BoundedReadOnlyExecutor
            self._bounded = BoundedReadOnlyExecutor(readonly_policy)

    def tools(self) -> tuple[ToolSpec, ...]:
        """Specs this runtime can execute, for adapters to advertise."""
        return tuple(spec for spec in self.registry
                     if spec.risk in EXECUTABLE_RISKS or spec.name in self._durable)

    def serves(self, name: object) -> bool:
        return any(spec.name == name for spec in self.tools())

    def permits(self, name: str) -> bool:
        spec = self.registry.get(name) if name in self.registry else None
        return self.authority.permits(spec.grant if spec is not None else name)

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
        if not self.authority.permits(spec.grant):
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
        try:
            spec = self._executable(name)
        except OperationError:
            return ExecutionResult(None, "INVALID_TOOL_RESULT", result.states)
        if not self.authority.permits(spec.grant):
            return ExecutionResult(None, "NOT_AUTHORIZED", result.states)
        try:
            spec.verify_result(spec.validate(arguments), result.result)
        except OperationError:
            return ExecutionResult(None, "INVALID_TOOL_RESULT", result.states)
        return result

    async def acall(self, name: str, arguments: Any) -> ToolOutcome:
        """The single async entry point for protocol adapters."""
        try:
            spec = self.registry.get(name)
        except OperationError as exc:
            return _failure(exc.code)
        if spec.risk in EXECUTABLE_RISKS:
            return await self._acall_read_only(name, arguments)
        if spec.name in self._durable:
            return await self._acall_durable(spec, arguments)
        return _failure("UNSUPPORTED_OPERATION")

    async def _acall_read_only(self, name: str, arguments: Any) -> ToolOutcome:
        # POLICY-011: bounded elapsed response time, including queue wait.
        try:
            if self._bounded is not None:
                result = await self._bounded.run(self.execute, name, arguments)
            else:
                import anyio
                result = await anyio.to_thread.run_sync(self.execute, name, arguments)
        except TimeoutError:
            return _failure("TIMEOUT")
        released = self.release(name, arguments, result)
        if not released.succeeded:
            return _failure(released.error_code)
        return ToolOutcome(True, released.result, None)

    async def _acall_durable(self, spec: ToolSpec, arguments: Any) -> ToolOutcome:
        try:
            trusted = spec.validate(arguments)
        except OperationError as exc:
            return _failure(exc.code)
        # INTEGRATION-002: never reserve or dispatch without the host grant.
        if not self.authority.permits(spec.grant):
            return _failure("NOT_AUTHORIZED")
        executor = self._durable[spec.name]
        # The SDK cancels this coroutine on client cancellation; the executor
        # owns ledger state transitions for that case (CANCEL-002..006).
        if hasattr(executor, "acall"):
            outcome = await executor.acall(spec.name, arguments)
        else:
            outcome = executor.call(spec.name, arguments)
            if inspect.isawaitable(outcome):
                outcome = await outcome
        if not outcome.ok:
            return _failure(outcome.error_code)
        # task.trust.007 / task.trust.011 at handoff, as for read-only tools.
        if not self.authority.permits(spec.grant):
            return _failure("NOT_AUTHORIZED")
        try:
            spec.verify_result(trusted, outcome.value)
        except OperationError:
            return _failure("INVALID_TOOL_RESULT")
        if (type(outcome.operation_id) is not str or not outcome.operation_id
                or type(outcome.replayed) is not bool):
            return _failure("INVALID_TOOL_RESULT")
        return ToolOutcome(True, outcome.value, None, outcome.operation_id, outcome.replayed)
