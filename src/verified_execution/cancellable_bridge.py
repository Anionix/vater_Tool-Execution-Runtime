"""Request-scoped, cooperative cancellation for a local pure calculator.

Machine contracts:
  CANCEL-001: SDK owns JSON-RPC request-ID matching and response suppression.
  CANCEL-002: cancellation while PREPARED records CANCELLED; no dispatch.
  CANCEL-003: cancellation after DISPATCHED records request intent and UNKNOWN.
  CANCEL-004: cancellation after successful COMMIT preserves durable success.
  CANCEL-005: only a fenced, live session may change DISPATCHED -> UNKNOWN.
  CANCEL-006: replay always returns durable result or a non-retryable state.
  CANCEL-010: blocking SQLite work never runs on the event-loop thread.
  CANCEL-011: cancellation during reservation or dispatch wait has a cleanup path.
  CANCEL-012: no abandoned background DB write on task cancellation.
  CANCEL-013: database busy/error never produces a fabricated success.

The callbacks are INJECTED BY THE HOST for deterministic offline tests, never
read from tool arguments. No network calls, subprocess effects, or MCP JSON-RPC
are implemented in this module. The SDK must cancel this coroutine on client
cancellation; only an actual SDK test can verify wire response suppression.
"""
from __future__ import annotations

import anyio
import sqlite3
import logging
from functools import partial
from collections.abc import Awaitable, Callable

from .core import LocalAuthority, OperationError, checked_add, validated_arguments
from .ledger import LedgerError, Operation
from .recorded_tool_bridge import BridgeResult, RECORDED_TOOL_NAME
from .worker_leases import DEFAULT_LEASE_MS, LeasedLedger
from .cancellation_journal import CancellationJournal, JournalError

Checkpoint = Callable[[Operation], Awaitable[None]]


class CancellableLeasedToolBridge:
    def __init__(self, ledger: LeasedLedger, *, principal: str,
                 authority: LocalAuthority, duration_ms: int = DEFAULT_LEASE_MS,
                 before_dispatch: Checkpoint | None = None,
                 after_dispatch: Checkpoint | None = None,
                 after_commit: Checkpoint | None = None,
                 cancellation_journal: CancellationJournal | None = None):
        self.ledger = ledger
        self.principal = principal
        self.authority = authority
        self.duration_ms = ledger._lease_duration(duration_ms)
        # Test-only, host-controlled checkpoint injection. Production defaults to None.
        self._before_dispatch = before_dispatch
        self._after_dispatch = after_dispatch
        self._after_commit = after_commit
        self._cancellation_journal = cancellation_journal
        # Bounded DB concurrency. Each awaited transaction is shielded until it
        # finishes: abandon_on_cancel=True would leave mutating SQL racing cleanup.
        self._db_limiter = anyio.CapacityLimiter(4)

    async def _database(self, function, /, *args, **kwargs):
        return await anyio.to_thread.run_sync(
            partial(function, *args, **kwargs),
            abandon_on_cancel=False, limiter=self._db_limiter)

    async def acall(self, name: str, arguments: object) -> BridgeResult:
        if name != RECORDED_TOOL_NAME:
            return BridgeResult(False, None, None, False, 'UNKNOWN_TOOL')
        if type(arguments) is not dict or set(arguments) != {'request_key', 'left', 'right'}:
            return BridgeResult(False, None, None, False, 'INVALID_ARGUMENT')
        key = arguments['request_key']
        if type(key) is not str or not key or '\x00' in key:
            return BridgeResult(False, None, None, False, 'INVALID_REQUEST_KEY')
        try:
            if len(key.encode('utf-8')) > 256:
                return BridgeResult(False, None, None, False, 'INVALID_REQUEST_KEY')
        except UnicodeError:
            return BridgeResult(False, None, None, False, 'INVALID_REQUEST_KEY')
        try:
            left, right = validated_arguments({'left': arguments['left'],
                                               'right': arguments['right']})
        except OperationError as exc:
            return BridgeResult(False, None, None, False, exc.code)
        if 'checked_add' not in self.authority.allowed_tools:
            return BridgeResult(False, None, None, False, 'NOT_AUTHORIZED')

        op: Operation | None = None
        session = None
        dispatched = False
        committed = False
        try:
            # SQLITE-WAIT-001: both operations are protected against cancellation
            # inside an uncommitted database transaction. The task loop stays free.
            session = await self._database(self.ledger.new_session,
                                           duration_ms=self.duration_ms)
            op = await self._database(
                self.ledger.reserve, principal=self.principal, request_key=key,
                tool_name='checked_add', target_id=None, resource_version=None,
                arguments={'left': left, 'right': right}, requires_approval=False)
            await anyio.lowlevel.checkpoint()
            if op.replayed:
                saved = await self._database(self.ledger.read_calculation,
                                             op.operation_id, principal=self.principal)
                await anyio.lowlevel.checkpoint()
                if saved is not None:
                    return BridgeResult(True, saved, op.operation_id, True, None)
                reason = {'prepared': 'IN_PROGRESS', 'dispatched': 'IN_PROGRESS',
                          'unknown': 'OUTCOME_UNKNOWN'}.get(op.state, 'ALREADY_RECORDED')
                return BridgeResult(False, None, op.operation_id, True, reason)

            if self._before_dispatch is not None:
                await self._before_dispatch(op)
            await anyio.lowlevel.checkpoint()

            # The task may have been cancelled while blocked inside SQLite.
            # A completed start() transaction is never forgotten: its result
            # is awaited before cancellation is acted on at the next checkpoint.
            await self._database(self.ledger.start, op.operation_id,
                                 token=session.token, duration_ms=self.duration_ms)
            dispatched = True
            await anyio.lowlevel.checkpoint()
            if self._after_dispatch is not None:
                await self._after_dispatch(op)
            await anyio.lowlevel.checkpoint()

            try:
                total = checked_add(left, right)
            except OperationError as error:
                await self._database(self.ledger.fail, op.operation_id,
                                     token=session.token)
                committed = True
                return BridgeResult(False, None, op.operation_id, False, error.code)

            await self._database(self.ledger.finish, op.operation_id,
                                 token=session.token, value=total)
            committed = True
            await anyio.lowlevel.checkpoint()
            if self._after_commit is not None:
                await self._after_commit(op)
            return BridgeResult(True, total, op.operation_id, False, None)

        except anyio.get_cancelled_exc_class():
            # SQLITE-WAIT-002: cleanup occurs even for cancellation while
            # reserving. The cancelled task must wait for any running SQL to
            # finish BEFORE cleanup; no fire-and-forget SQL is permitted.
            if op is not None and not op.replayed and not committed:
                with anyio.CancelScope(shield=True):
                    persisted = False
                    if self._cancellation_journal is not None:
                        # CANCEL-DURABLE-001: fsync intent before SQLite cleanup.
                        # A failure to fsync must never be silently called durable.
                        try:
                            await self._database(self._cancellation_journal.persist,
                                                 op.operation_id, principal=self.principal)
                            persisted = True
                        except (JournalError, OSError):
                            # CANCEL-DURABLE-003: do not silently claim resilience
                            # when both SQLite and independent storage may fail.
                            logging.getLogger(__name__).error(
                                'CANCEL_INTENT_PERSIST_FAILED; operator review required')
                    try:
                        if not dispatched:
                            await self._database(self.ledger.request_cancel,
                                                 op.operation_id)
                        elif session is not None:
                            await self._database(self.ledger.abandon_owner_on_cancel,
                                                 op.operation_id, token=session.token)
                    except (LedgerError, sqlite3.Error):
                        # Persisted intent is recoverable when SQLite becomes writable.
                        # No DB success or rollback is claimed here.
                        pass
                    else:
                        if persisted:
                            try:
                                await self._database(self._cancellation_journal.replay, self.ledger)
                            except (JournalError, LedgerError, sqlite3.Error, OSError):
                                pass
            raise

        except LedgerError as error:
            if op is not None and not op.replayed and not dispatched:
                with anyio.CancelScope(shield=True):
                    try:
                        await self._database(self.ledger.request_cancel, op.operation_id)
                    except (LedgerError, sqlite3.Error):
                        pass
            return BridgeResult(False, None, op.operation_id if op else None,
                                bool(op.replayed) if op else False, error.code)

        except sqlite3.Error as error:
            # SQLITE-WAIT-003: errors from BEGIN IMMEDIATE may mean "busy",
            # but after dispatch the outcome could be uncertain. The adapter
            # does not retry or invent a result.
            if op is not None and not op.replayed and not committed:
                with anyio.CancelScope(shield=True):
                    try:
                        if dispatched and session is not None:
                            await self._database(self.ledger.mark_owner_unknown,
                                                 op.operation_id, token=session.token)
                        elif not dispatched:
                            await self._database(self.ledger.request_cancel,
                                                 op.operation_id)
                    except (LedgerError, sqlite3.Error):
                        pass
            code = ('DATABASE_BUSY' if getattr(error, 'sqlite_errorcode', None) ==
                    sqlite3.SQLITE_BUSY or 'locked' in str(error).lower()
                    else 'DATABASE_ERROR')
            return BridgeResult(False, None, op.operation_id if op else None,
                                bool(op.replayed) if op else False, code)
