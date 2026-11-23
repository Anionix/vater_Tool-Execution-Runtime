"""Multi-worker, local SQLite fencing for a pure calculator tool.

STATE-MACHINE CONTRACT:
  LEASE-001: sessions are host-created random tokens, not client request fields.
  LEASE-002: only an unexpired session can move prepared -> dispatched.
  LEASE-003: operation lease, dispatch event and state are one transaction.
  LEASE-004: owner token AND session expiry AND per-operation expiry must match
             before a worker can commit a result or report unknown.
  LEASE-005: recovery only quarantines expired leased operations, never another
             worker's unexpired operation or legacy (unleased) dispatches.
  LEASE-006: an expired worker cannot re-acquire its old session; a new session
             cannot execute an operation left by the old one automatically.
  LEASE-007: cancellation after dispatch is a request, not a rollback.
  CANCEL-005: only the live fenced owner can mark a post-dispatch cancel request.

This is a deliberately narrow local multi-process prototype. Wall-clock jumps,
shared filesystems, malicious local code, trusted-identity issuance, and remote
exactly-once execution are outside these guarantees.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .request_cancellation import RequestCancellation

from .core import LocalAuthority, OperationError, checked_add, validated_arguments
from .ledger import OperationLedger, LedgerError, _text
from .recorded_calculator import RecordedResult

DEFAULT_LEASE_MS = 30_000


@dataclass(frozen=True)
class WorkerSession:
    token: str
    expires_at_ms: int


class LeasedLedger(OperationLedger):
    def __init__(self, path, *, timeout_seconds: float = 5.0):
        super().__init__(path, timeout_seconds=timeout_seconds)
        with self._open() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS worker_sessions (
                    session_token TEXT PRIMARY KEY,
                    expiry_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operation_leases (
                    operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
                    session_token TEXT NOT NULL REFERENCES worker_sessions(session_token),
                    expiry_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS operation_leases_by_expiry
                  ON operation_leases(expiry_ms);
            """)

    @staticmethod
    def _lease_duration(value: int) -> int:
        if type(value) is not int or not 50 <= value <= 3_600_000:
            raise LedgerError("INVALID_LEASE_DURATION")
        return value

    def new_session(self, *, duration_ms: int = DEFAULT_LEASE_MS) -> WorkerSession:
        self._lease_duration(duration_ms)
        now = self._now()
        token = str(uuid4())
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO worker_sessions(session_token,expiry_ms) VALUES (?,?)",
                (token, now + duration_ms))
        return WorkerSession(token, now + duration_ms)

    def _assert_owner(self, conn, operation_id: str, token: str, now: int) -> None:
        row = conn.execute("""
            SELECT w.expiry_ms, l.expiry_ms
            FROM operation_leases l JOIN worker_sessions w
            ON l.session_token=w.session_token
            WHERE l.operation_id=? AND l.session_token=?
        """, (operation_id, token)).fetchone()
        if row is None or min(row) <= now:
            raise LedgerError("STALE_OR_DIFFERENT_OWNER")

    def _assert_live_session(self, conn, token: str, now: int) -> None:
        row = conn.execute("SELECT expiry_ms FROM worker_sessions WHERE session_token=?", (token,)).fetchone()
        if row is None or row[0] <= now:
            raise LedgerError("SESSION_EXPIRED")

    def heartbeat(self, token: str, *, duration_ms: int = DEFAULT_LEASE_MS) -> WorkerSession:
        token = _text("session_token", token)
        self._lease_duration(duration_ms)
        now = self._now()
        expiry = now + duration_ms
        with self._transaction() as conn:
            self._assert_live_session(conn, token, now)
            conn.execute("UPDATE worker_sessions SET expiry_ms=? WHERE session_token=?", (expiry, token))
            # All uncompleted operations leased to this session are extended.
            conn.execute("""
                UPDATE operation_leases SET expiry_ms=? WHERE session_token=?
                AND expiry_ms>? AND operation_id IN
                   (SELECT operation_id FROM operations WHERE state='dispatched')
            """, (expiry, token, now))
        return WorkerSession(token, expiry)

    def start(self, operation_id: str, *, token: str, duration_ms: int = DEFAULT_LEASE_MS):
        """Transactional PREPARED -> DISPATCHED with a fenced lease. No approvals.

        Caller must run business policy before invoking this trusted operation.
        """
        operation_id = _text("operation_id", operation_id)
        token = _text("session_token", token)
        self._lease_duration(duration_ms)
        now = self._now()
        with self._transaction() as conn:
            self._assert_live_session(conn, token, now)
            session_exp = conn.execute("SELECT expiry_ms FROM worker_sessions WHERE session_token=?", (token,)).fetchone()[0]
            op = self._get(conn, operation_id)
            if op.state != 'prepared' or op.cancel_requested:
                raise LedgerError("NOT_PREPARED")
            if op.requires_approval or op.tool_name != 'checked_add':
                raise LedgerError("UNSUPPORTED_OPERATION")
            expiry = min(session_exp, now + duration_ms)
            conn.execute(
                "INSERT INTO operation_leases(operation_id,session_token,expiry_ms) VALUES (?,?,?)",
                (operation_id, token, expiry))
            changed = conn.execute("""
                UPDATE operations SET state='dispatched',updated_at_ms=?
                WHERE operation_id=? AND state='prepared' AND cancel_requested=0
            """, (now, operation_id))
            if changed.rowcount != 1:
                raise LedgerError("NOT_PREPARED")
            self._event(conn, operation_id, 'leased_dispatch', now)
            return self._get(conn, operation_id)

    def finish(self, operation_id: str, *, token: str, value: int):
        """Atomic result persistence & owner verification (pure calculation only)."""
        operation_id = _text("operation_id", operation_id)
        token = _text("session_token", token)
        if type(value) is not int or not 0 <= value <= 4294967295:
            raise LedgerError("INVALID_CALCULATION_RESULT")
        now = self._now()
        with self._transaction() as conn:
            self._assert_owner(conn, operation_id, token, now)
            op = self._get(conn, operation_id)
            if op.state != 'dispatched' or op.tool_name != 'checked_add':
                raise LedgerError("NOT_DISPATCHED")
            conn.execute("INSERT INTO calculation_results(operation_id,result) VALUES (?,?)", (operation_id,value))
            count = conn.execute(
                "UPDATE operations SET state='succeeded',updated_at_ms=? WHERE operation_id=? AND state='dispatched'",
                (now, operation_id)).rowcount
            if count != 1:
                raise LedgerError("NOT_DISPATCHED")
            self._event(conn, operation_id, 'leased_completed', now)
            return self._get(conn, operation_id)

    def fail(self, operation_id: str, *, token: str):
        """Record a *definitive*, owner-authorized local calculation failure."""
        operation_id = _text('operation_id', operation_id)
        token = _text('session_token', token)
        now = self._now()
        with self._transaction() as conn:
            self._assert_owner(conn, operation_id, token, now)
            op = self._get(conn, operation_id)
            if op.state != 'dispatched' or op.tool_name != 'checked_add':
                raise LedgerError('NOT_DISPATCHED')
            conn.execute("UPDATE operations SET state='failed',updated_at_ms=? WHERE operation_id=?",
                         (now, operation_id))
            self._event(conn, operation_id, 'leased_definitive_failed', now)
            return self._get(conn, operation_id)

    def mark_owner_unknown(self, operation_id: str, *, token: str):
        operation_id = _text("operation_id", operation_id)
        token = _text("session_token", token)
        now = self._now()
        with self._transaction() as conn:
            self._assert_owner(conn, operation_id, token, now)
            op = self._get(conn, operation_id)
            if op.state != 'dispatched':
                raise LedgerError('NOT_DISPATCHED')
            conn.execute("UPDATE operations SET state='unknown',updated_at_ms=? WHERE operation_id=?", (now,operation_id))
            self._event(conn, operation_id, 'owner_outcome_unknown', now)
            return self._get(conn, operation_id)

    def owner_cancel(self, operation_id: str, *, token: str):
        """CANCEL-005: only a live fenced owner can request post-dispatch cancel.

        Does not alter operation state and does not imply rollback.
        The request's async task is responsible for discarding its response.
        """
        operation_id = _text("operation_id", operation_id)
        token = _text("session_token", token)
        now = self._now()
        with self._transaction() as conn:
            self._assert_owner(conn, operation_id, token, now)
            op = self._get(conn, operation_id)
            if op.state != 'dispatched':
                raise LedgerError('NOT_DISPATCHED')
            if not op.cancel_requested:
                conn.execute("UPDATE operations SET cancel_requested=1,updated_at_ms=? "
                             "WHERE operation_id=? AND state='dispatched'",
                             (now, operation_id))
                self._event(conn, operation_id, 'owner_cancel_requested', now)
            return self._get(conn, operation_id)

    def abandon_owner_on_cancel(self, operation_id: str, *, token: str):
        """CANCEL-009: owner-checked dispatch abandonment is a single transaction.

        A cancelled in-flight handler does NOT know whether the dispatched work
        executed; conservatively quarantine as UNKNOWN, not CANCELLED. Never
        retry without external definitive evidence. Cancel after success is
        a no-op for terminal state and must be handled by its caller.
        """
        operation_id = _text('operation_id', operation_id)
        token = _text('session_token', token)
        now = self._now()
        with self._transaction() as conn:
            self._assert_owner(conn, operation_id, token, now)
            op = self._get(conn, operation_id)
            if op.state != 'dispatched':
                raise LedgerError('NOT_DISPATCHED')
            changed = conn.execute(
                "UPDATE operations SET state='unknown',cancel_requested=1,updated_at_ms=? "
                "WHERE operation_id=? AND state='dispatched'", (now,operation_id))
            if changed.rowcount != 1:
                raise LedgerError('NOT_DISPATCHED')
            self._event(conn,operation_id,'owner_cancelled_outcome_unknown',now)
            return self._get(conn,operation_id)

    def recover_expired(self) -> int:
        """Quarantine only stale fenced dispatches, NEVER active or legacy ops."""
        now = self._now()
        with self._transaction() as conn:
            rows = conn.execute("""
                SELECT l.operation_id FROM operation_leases l
                JOIN worker_sessions w ON l.session_token=w.session_token
                JOIN operations o ON o.operation_id=l.operation_id
                WHERE o.state='dispatched' AND (l.expiry_ms<=? OR w.expiry_ms<=?)
                ORDER BY l.operation_id
            """, (now,now)).fetchall()
            for (operation_id,) in rows:
                conn.execute("UPDATE operations SET state='unknown',updated_at_ms=? WHERE operation_id=?",(now,operation_id))
                self._event(conn, operation_id, 'expired_owner_outcome_unknown', now)
            return len(rows)

    def legacy_recovery(self):
        """Block the unsafe global recovery API on the multi-worker variant."""
        raise LedgerError('USE_EXPIRED_LEASE_RECOVERY')

    def recover_interrupted(self):
        return self.legacy_recovery()

    def begin_dispatch(self, operation_id: str, *, approval_id: str | None = None):
        """Old API is incompatible with fenced multi-worker execution."""
        raise LedgerError('OWNER_REQUIRED')

    def complete_calculation(self, operation_id: str, value: int):
        raise LedgerError('OWNER_REQUIRED')

    def record_definitive_result(self, operation_id: str, result: str):
        raise LedgerError('OWNER_REQUIRED')

    def mark_unknown(self, operation_id: str):
        raise LedgerError('OWNER_REQUIRED')


class LeasedCalculator:
    """Host-configured worker lease + pure arithmetic, no remote mutations."""
    def __init__(self, ledger: LeasedLedger, *, principal: str,
                 authority: LocalAuthority, session: WorkerSession):
        self.ledger = ledger
        self.principal = principal
        self.authority = authority
        self.session = session

    def execute(self, *, request_key: str, name: str, arguments,
                cancellation: "RequestCancellation | None" = None) -> RecordedResult:
        try:
            if name != 'checked_add':
                raise OperationError('UNKNOWN_TOOL','unknown tool')
            left,right = validated_arguments(arguments)
        except OperationError as exc:
            return RecordedResult(False,None,exc.code,None,None)
        if name not in self.authority.allowed_tools:
            return RecordedResult(False,None,'NOT_AUTHORIZED',None,None)
        if cancellation is not None and cancellation.cancelled:
            return RecordedResult(False,None,'CANCELLED',None,None)
        try:
            op = self.ledger.reserve(principal=self.principal,request_key=request_key,
                    tool_name=name,target_id=None,resource_version=None,
                    arguments={'left':left,'right':right},requires_approval=False)
            if op.replayed:
                saved=self.ledger.read_calculation(op.operation_id,principal=self.principal)
                if saved is not None:
                    return RecordedResult(True,saved,None,op.operation_id,'succeeded',True)
                code={'prepared':'IN_PROGRESS','dispatched':'IN_PROGRESS','unknown':'OUTCOME_UNKNOWN'}.get(op.state,'ALREADY_RECORDED')
                return RecordedResult(False,None,code,op.operation_id,op.state,True)
            # CANCEL-002: replayed requests never reach this binding.
            if cancellation is not None:
                cancellation.bind_new_operation(self.ledger, op.operation_id)
                if cancellation.cancelled:
                    return RecordedResult(False, None, 'CANCELLED',
                                          op.operation_id, 'cancelled')
            self.ledger.start(op.operation_id,token=self.session.token)
        except LedgerError as exc:
            if cancellation is not None and cancellation.cancelled:
                return RecordedResult(False,None,'CANCELLED',
                                      op.operation_id if 'op' in locals() else None,'cancelled')
            return RecordedResult(False,None,exc.code,None,None)
        try:
            total=checked_add(left,right)
        except OperationError as exc:
            # Overflow is a definitive local error, not an ambiguous external action.
            try:
                self.ledger.fail(op.operation_id,token=self.session.token)
                return RecordedResult(False,None,exc.code,op.operation_id,'failed')
            except LedgerError as owner_error:
                return RecordedResult(False,None,owner_error.code,op.operation_id,'dispatched')
        try:
            self.ledger.finish(op.operation_id,token=self.session.token,value=total)
        except LedgerError as exc:
            # Expired owner is not allowed to claim a result; wait for recovery.
            return RecordedResult(False,None,exc.code,op.operation_id,'dispatched')
        return RecordedResult(True,total,None,op.operation_id,'succeeded')


class LeasedToolBridge:
    """Optional multi-worker MCP application bridge with per-request session.

    No long-lived heartbeat is needed for this tiny, pure calculator: each call
    gets a fresh lease, and an expired call is never retried automatically.
    For long-running operations, use a maintained heartbeat or task leases.
    """
    def __init__(self, ledger: LeasedLedger, *, principal: str,
                 authority: LocalAuthority, duration_ms: int = DEFAULT_LEASE_MS):
        self.ledger = ledger
        self.principal = principal
        self.authority = authority
        self.duration_ms = ledger._lease_duration(duration_ms)

    def call(self, name: str, arguments, cancellation: "RequestCancellation | None" = None):
        from .recorded_tool_bridge import BridgeResult, RECORDED_TOOL_NAME
        if name != RECORDED_TOOL_NAME:
            return BridgeResult(False,None,None,False,'UNKNOWN_TOOL')
        if type(arguments) is not dict or set(arguments) != {'request_key','left','right'}:
            return BridgeResult(False,None,None,False,'INVALID_ARGUMENT')
        key=arguments['request_key']
        if type(key) is not str or not key or '\x00' in key:
            return BridgeResult(False,None,None,False,'INVALID_REQUEST_KEY')
        try:
            if len(key.encode('utf-8')) > 256:
                return BridgeResult(False,None,None,False,'INVALID_REQUEST_KEY')
        except UnicodeError:
            return BridgeResult(False,None,None,False,'INVALID_REQUEST_KEY')
        try:
            validated_arguments({'left': arguments['left'], 'right': arguments['right']})
        except OperationError as exc:
            return BridgeResult(False,None,None,False,exc.code)
        if 'checked_add' not in self.authority.allowed_tools:
            return BridgeResult(False,None,None,False,'NOT_AUTHORIZED')
        if cancellation is not None and cancellation.cancelled:
            return BridgeResult(False,None,None,False,'CANCELLED')
        try:
            session=self.ledger.new_session(duration_ms=self.duration_ms)
            calculator=LeasedCalculator(self.ledger,principal=self.principal,
                        authority=self.authority,session=session)
            result=calculator.execute(request_key=key,name='checked_add',
                                     arguments={'left':arguments['left'],'right':arguments['right']},
                                     cancellation=cancellation)
        except LedgerError as exc:
            return BridgeResult(False,None,None,False,exc.code)
        return BridgeResult(result.ok,result.value,result.operation_id,
                            result.replayed,result.error_code)
