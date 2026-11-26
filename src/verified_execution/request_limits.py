"""Initial read-only operation budgets: SDK-independent and directly testable.

STATE MACHINE: WAITING_FOR_SLOT -> RUNNING -> FINISHED | TIMED_OUT | CANCELLED.
POLICY-010: byte limit enforced at the separately launched stdio gateway.
POLICY-011: timeout includes time spent waiting for a concurrency slot.
POLICY-012: limiter is one object per server instance; initial limit is ONE.
SECURITY: Only apply abandon_on_cancel to pure, bounded operations with no
side effects. It stops waiting, NOT a Python thread already running.
"""
from __future__ import annotations
from dataclasses import dataclass
from threading import BoundedSemaphore
import anyio

@dataclass(frozen=True)
class ReadOnlyPolicy:
    timeout_ms: int
    max_concurrent: int

    def __post_init__(self):
        if type(self.timeout_ms) is not int or not 1 <= self.timeout_ms <= 60000:
            raise ValueError("INVALID_TIMEOUT_POLICY")
        if type(self.max_concurrent) is not int or not 1 <= self.max_concurrent <= 32:
            raise ValueError("INVALID_CONCURRENCY_POLICY")


class CapacityExhausted(TimeoutError):
    """A timed-out calculation is still occupying its worker slot."""


class BoundedReadOnlyExecutor:
    def __init__(self, policy: ReadOnlyPolicy):
        self.policy = policy
        self._limiter = anyio.CapacityLimiter(policy.max_concurrent)
        self._workers = BoundedSemaphore(policy.max_concurrent)

    def _execute(self, callable_, args):
        # task.runtime.011: abandoning a response cannot free a still-running worker.
        if not self._workers.acquire(blocking=False):
            raise CapacityExhausted()
        try:
            return callable_(*args)
        finally:
            self._workers.release()

    async def run(self, callable_, *args):
        """Enforce elapsed deadline for queue wait + execution response.

        Underlying thread cannot be forcibly stopped; callable_ MUST be
        a trusted side-effect-free calculation and cannot execute client code.
        """
        with anyio.fail_after(self.policy.timeout_ms / 1000):
            async with self._limiter:
                return await anyio.to_thread.run_sync(
                    self._execute, callable_, args, abandon_on_cancel=True)
