"""Cooperative request cancellation token, not an MCP transport implementation.

STATE-MACHINE CONTRACT:
  CANCEL-001: cancellation is sticky; it never means external rollback.
  CANCEL-002: only a newly reserved operation can bind to the token.
  CANCEL-003: pre-dispatch cancellation records `cancelled` under SQLite lock.
  CANCEL-004: post-dispatch cancellation only sets cancel_requested.
  CANCEL-005: cancellation never claims a remote operation was undone.
  CANCEL-006: SDK response suppression is SDK responsibility and remains untested.

The token is created per inbound call by trusted server code; it is not a
client-specified operation identifier or long-lived idempotency key.
"""
from __future__ import annotations

import threading
from typing import Protocol


class CancellationLedger(Protocol):
    def request_cancel(self, operation_id: str): ...


class RequestCancellation:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._bound: tuple[CancellationLedger, str] | None = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        """Notify running business code; cancellation is idempotent.

        The event is set FIRST so the worker observes cancellation even if
        the database callback is delayed by a locked SQLite writer.
        """
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            bound = self._bound
        if bound is not None:
            ledger, operation_id = bound
            ledger.request_cancel(operation_id)

    def bind_new_operation(self, ledger: CancellationLedger, operation_id: str) -> None:
        """Bind only the operation newly created by this request (not replays).

        On cancellation that happened between reserving and binding, this
        immediately asks the database to cancel. SQLite serializes this
        with the corresponding prepared->dispatched transition.
        """
        with self._lock:
            if self._bound is not None:
                raise RuntimeError("REQUEST_ALREADY_BOUND")
            self._bound = (ledger, operation_id)
            was_cancelled = self._event.is_set()
        if was_cancelled:
            ledger.request_cancel(operation_id)
