"""Durable local operation ledger. Not a remote exactly-once guarantee.

State-machine contracts:
  LEDGER-001: (principal, request_key) maps to one immutable operation fingerprint.
  LEDGER-002: at most one successful prepared -> dispatched transition.
  LEDGER-003: an approval binds principal, tool, target, resource version and arguments.
  LEDGER-004: approval can be consumed by only one operation before expiration.
  LEDGER-005: cancellation after dispatch is only a *request*, never an undo.
  LEDGER-006: after crash, in-flight dispatch becomes UNKNOWN, never auto-retried.
  LEDGER-007: UNKNOWN can resolve only with definitive external evidence.
  LEDGER-008: no raw tool arguments or approval secrets are persisted.
  LEDGER-009: successful pure-calculator output and completion commit atomically.
  LEDGER-010: cached output is readable only for the same principal and operation.

Trust boundary: approval issuance, authentication and side effects are NOT implemented
here. Only privileged host code may call issue_approval or begin_dispatch.
Do NOT expose ledger methods as unauthenticated network or MCP tools.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator, Literal, NamedTuple
from uuid import uuid4

from .core import LocalAuthority

STATES = frozenset({"prepared", "dispatched", "succeeded", "failed", "unknown", "cancelled"})
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


class ResourceSnapshot(NamedTuple):
    owner: str
    version: str


class LedgerError(Exception):
    """Stable machine-readable failure; external clients should not see SQL details."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Operation:
    operation_id: str
    principal: str
    request_key: str
    tool_name: str
    target_id: str
    resource_version: str
    arguments_digest: str
    requires_approval: bool
    state: str
    cancel_requested: bool
    replayed: bool = False


def _text(name: str, value: Any, max_bytes: int = 256) -> str:
    # TEXT-001: every malformed caller-controlled identifier fails with a
    # stable LedgerError, never a raw UnicodeEncodeError containing input.
    code = "INVALID_" + name.upper()
    if type(value) is not str or not value or "\x00" in value:
        raise LedgerError(code)
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise LedgerError(code) from None
    if len(encoded) > max_bytes:
        raise LedgerError(code)
    return value


@dataclass(frozen=True)
class HostPrincipal:
    """A local host binding. The public request schema never accepts this object."""
    name: str

    def __post_init__(self) -> None:
        _text("principal", self.name)


def _optional_text(name: str, value: Any) -> str:
    return "" if value is None else _text(name, value)


def fingerprint(arguments: Any) -> str:
    """Stable digest for JSON-compatible data. Does not validate business semantics.

    Duplicate JSON keys MUST be rejected before parsing, elsewhere in the gateway.
    Float values are excluded to prevent type/equality surprises across languages.
    """
    def validate(value: Any, depth: int = 0) -> None:
        if depth > 32:
            raise LedgerError("INVALID_ARGUMENTS")
        if value is None or type(value) in (str, bool, int):
            return
        if isinstance(value, list):
            for child in value:
                validate(child, depth + 1)
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if type(key) is not str:
                    raise LedgerError("INVALID_ARGUMENTS")
                validate(child, depth + 1)
            return
        raise LedgerError("INVALID_ARGUMENTS")

    validate(arguments)
    try:
        canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
        raise LedgerError("INVALID_ARGUMENTS") from exc
    if len(canonical) > 16_384:
        raise LedgerError("ARGUMENTS_TOO_LARGE")
    return hashlib.sha256(canonical).hexdigest()


class OperationLedger:
    def __init__(self, path: str | Path, *, timeout_seconds: float = 5.0,
                 read_resource: Callable[[str], ResourceSnapshot | None] | None = None):
        self.read_resource = read_resource
        self.path = str(path)
        if self.path == ":memory:":
            raise LedgerError("FILE_DATABASE_REQUIRED")
        if not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 60:
            raise LedgerError("INVALID_TIMEOUT")
        self.timeout_seconds = timeout_seconds
        with self._open() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    principal TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    resource_version TEXT NOT NULL,
                    arguments_digest TEXT NOT NULL,
                    requires_approval INTEGER NOT NULL CHECK (requires_approval IN (0,1)),
                    state TEXT NOT NULL CHECK (state IN ('prepared','dispatched','succeeded','failed','unknown','cancelled')),
                    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)),
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    UNIQUE(principal,request_key)
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    principal TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    resource_version TEXT NOT NULL,
                    arguments_digest TEXT NOT NULL,
                    expires_at_ms INTEGER NOT NULL,
                    consumed_by TEXT UNIQUE REFERENCES operations(operation_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
                    event_name TEXT NOT NULL,
                    event_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS calculation_results (
                    operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
                    result INTEGER NOT NULL CHECK (result BETWEEN 0 AND 4294967295)
                );
            """)

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, isolation_level=None,
                                     timeout=self.timeout_seconds)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _now() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _row(row: tuple, *, replayed: bool = False) -> Operation:
        return Operation(*row[:7], bool(row[7]), row[8], bool(row[9]), replayed)

    @staticmethod
    def _get(connection: sqlite3.Connection, operation_id: str) -> Operation:
        row = connection.execute("""
            SELECT operation_id,principal,request_key,tool_name,target_id,
                   resource_version,arguments_digest,requires_approval,state,cancel_requested
            FROM operations WHERE operation_id = ?
        """, (operation_id,)).fetchone()
        if row is None:
            raise LedgerError("UNKNOWN_OPERATION")
        return OperationLedger._row(row)

    def get(self, operation_id: str) -> Operation:
        with self._open() as connection:
            return self._get(connection, _text("operation_id", operation_id))

    @staticmethod
    def _event(connection: sqlite3.Connection, operation_id: str, name: str, now: int) -> None:
        connection.execute(
            "INSERT INTO events(operation_id,event_name,event_at_ms) VALUES (?,?,?)",
            (operation_id, name, now))

    def reserve(self, *, principal: str, request_key: str, tool_name: str,
                target_id: str | None, resource_version: str | None,
                arguments: Any, requires_approval: bool = True) -> Operation:
        """Create one prepared operation; same request key+fingerprint replays a record.

        Reservation does NOT authorize dispatch and does NOT execute side effects.
        """
        principal = _text("principal", principal)
        request_key = _text("request_key", request_key)
        tool_name = _text("tool_name", tool_name)
        target_id = _optional_text("target_id", target_id)
        resource_version = _optional_text("resource_version", resource_version)
        if type(requires_approval) is not bool:
            raise LedgerError("INVALID_APPROVAL_FLAG")
        arguments_digest = fingerprint(arguments)
        now = self._now()
        with self._transaction() as connection:
            row = connection.execute("""
                SELECT operation_id,principal,request_key,tool_name,target_id,
                       resource_version,arguments_digest,requires_approval,state,cancel_requested
                FROM operations WHERE principal=? AND request_key=?
            """, (principal, request_key)).fetchone()
            if row:
                existing = self._row(row, replayed=True)
                if (existing.tool_name, existing.target_id, existing.resource_version,
                    existing.arguments_digest, existing.requires_approval) != (
                        tool_name, target_id, resource_version, arguments_digest, requires_approval):
                    raise LedgerError("IDEMPOTENCY_CONFLICT")
                return existing
            operation_id = str(uuid4())
            connection.execute("""
                INSERT INTO operations(operation_id,principal,request_key,tool_name,target_id,
                  resource_version,arguments_digest,requires_approval,state,created_at_ms,updated_at_ms)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """, (operation_id, principal, request_key, tool_name, target_id,
                  resource_version, arguments_digest, int(requires_approval), "prepared", now, now))
            self._event(connection, operation_id, "reserved", now)
            return self._get(connection, operation_id)

    def issue_approval(self, *, approval_id: str, principal: str, tool_name: str,
                       target_id: str | None, resource_version: str | None,
                       arguments: Any, expires_at_ms: int) -> None:
        """Privileged host-only action; NOT proof that a human approved anything."""
        vals = (_text("approval_id", approval_id), _text("principal", principal),
                _text("tool_name", tool_name), _optional_text("target_id", target_id),
                _optional_text("resource_version", resource_version), fingerprint(arguments))
        if type(expires_at_ms) is not int or expires_at_ms <= self._now():
            raise LedgerError("INVALID_EXPIRY")
        try:
            with self._transaction() as connection:
                connection.execute("""
                    INSERT INTO approvals(approval_id,principal,tool_name,target_id,
                        resource_version,arguments_digest,expires_at_ms)
                    VALUES (?,?,?,?,?,?,?)
                """, (*vals, expires_at_ms))
        except sqlite3.IntegrityError as exc:
            raise LedgerError("DUPLICATE_APPROVAL") from exc

    @staticmethod
    def _guard_legacy_write(connection: sqlite3.Connection, operation_id: str) -> None:
        """Do not bypass owner fencing via the legacy API on a shared database.

        This guards accidental mixed-version application calls; malicious local
        database writers and direct SQL are outside this trust boundary.
        """
        has_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operation_leases'"
        ).fetchone()
        if has_table and connection.execute(
            "SELECT 1 FROM operation_leases WHERE operation_id=?", (operation_id,)
        ).fetchone():
            raise LedgerError('OWNER_REQUIRED')

    def revoke_approval(self, approval_id: str) -> bool:
        """Expire an unused host approval. Keep its identifier reserved across restarts."""
        approval_id = _text("approval_id", approval_id)
        with self._transaction() as connection:
            changed = connection.execute("""
                UPDATE approvals SET expires_at_ms=0
                WHERE approval_id=? AND consumed_by IS NULL AND expires_at_ms>0
            """, (approval_id,))
            return changed.rowcount == 1

    def _check_dispatch_grant(self, operation: Operation, authority: LocalAuthority | None) -> None:
        # task.contracts.007: under the dispatch lock, before consuming approval.
        if self.read_resource is not None and operation.target_id:
            current = self.read_resource(operation.target_id)
            if current is None or current.owner != operation.principal:
                raise LedgerError("NOT_AUTHORIZED")
            if not operation.resource_version or current.version != operation.resource_version:
                raise LedgerError("RESOURCE_VERSION_CHANGED")
        if authority is not None and not authority.permits(operation.tool_name):
            raise LedgerError("NOT_AUTHORIZED")

    def begin_dispatch(self, operation_id: str, *, approval_id: str | None = None,
                       authority: LocalAuthority | None = None) -> Operation:
        """Durable PREPARED -> DISPATCHED; call *before* external action.

        Dispatch may happen zero times even after this transition (process crash).
        Only a remote idempotency key + reconciliation can provide stronger claims.
        """
        operation_id = _text("operation_id", operation_id)
        with self._transaction() as connection:
            self._guard_legacy_write(connection, operation_id)
            op = self._get(connection, operation_id)
            if op.state != "prepared" or op.cancel_requested:
                raise LedgerError("NOT_PREPARED")
            self._check_dispatch_grant(op, authority)
            # Compare expiry after acquiring the writer lock and reading the target version.
            now = self._now()
            if op.requires_approval:
                approval_id = _text("approval_id", approval_id)
                record = connection.execute("""
                    SELECT principal,tool_name,target_id,resource_version,
                           arguments_digest,expires_at_ms,consumed_by
                    FROM approvals WHERE approval_id=?
                """, (approval_id,)).fetchone()
                if (record is None or record[-1] is not None or now >= record[-2]
                    or tuple(record[:5]) != (op.principal, op.tool_name, op.target_id,
                                              op.resource_version, op.arguments_digest)):
                    raise LedgerError("APPROVAL_INVALID")
                connection.execute("UPDATE approvals SET consumed_by=? WHERE approval_id=?",
                                   (operation_id, approval_id))
            elif approval_id is not None:
                raise LedgerError("APPROVAL_UNEXPECTED")
            result = connection.execute("""
                UPDATE operations SET state='dispatched',updated_at_ms=?
                WHERE operation_id=? AND state='prepared' AND cancel_requested=0
            """, (now, operation_id))
            if result.rowcount != 1:
                raise LedgerError("NOT_PREPARED")
            self._event(connection, operation_id, "dispatched", now)
            return self._get(connection, operation_id)

    def request_cancel(self, operation_id: str) -> Operation:
        """Before dispatch: cancel. After dispatch: flag only; no rollback guarantee."""
        operation_id = _text("operation_id", operation_id)
        now = self._now()
        with self._transaction() as connection:
            op = self._get(connection, operation_id)
            if op.state == "prepared":
                connection.execute("UPDATE operations SET state='cancelled', updated_at_ms=? WHERE operation_id=?",
                                   (now, operation_id))
                self._event(connection, operation_id, "cancelled_before_dispatch", now)
            elif op.state == "dispatched" and not op.cancel_requested:
                connection.execute("UPDATE operations SET cancel_requested=1, updated_at_ms=? WHERE operation_id=?",
                                   (now, operation_id))
                self._event(connection, operation_id, "cancel_requested", now)
            return self._get(connection, operation_id)

    def record_definitive_result(self, operation_id: str,
                                 result: Literal["succeeded", "failed"]) -> Operation:
        """Call only when the result is definitive, *not* merely a lost connection."""
        operation_id = _text("operation_id", operation_id)
        if result not in ("succeeded", "failed"):
            raise LedgerError("INVALID_RESULT")
        now = self._now()
        with self._transaction() as connection:
            self._guard_legacy_write(connection, operation_id)
            op = self._get(connection, operation_id)
            if op.state != "dispatched":
                raise LedgerError("NOT_DISPATCHED")
            connection.execute("UPDATE operations SET state=?,updated_at_ms=? WHERE operation_id=?",
                               (result, now, operation_id))
            self._event(connection, operation_id, "definitive_" + result, now)
            return self._get(connection, operation_id)

    def complete_calculation(self, operation_id: str, value: int) -> Operation:
        """LEDGER-009: persist output and success in ONE database transaction.

        Only applies to the trusted pure calculator. Never treat a remote side
        effect as confirmed using this helper.
        """
        operation_id = _text("operation_id", operation_id)
        if type(value) is not int or not 0 <= value <= 4294967295:
            raise LedgerError("INVALID_CALCULATION_RESULT")
        now = self._now()
        with self._transaction() as connection:
            self._guard_legacy_write(connection, operation_id)
            op = self._get(connection, operation_id)
            if op.state != "dispatched" or op.tool_name != "checked_add":
                raise LedgerError("NOT_CALCULATION_DISPATCHED")
            connection.execute(
                "INSERT INTO calculation_results(operation_id,result) VALUES (?,?)",
                (operation_id, value))
            updated = connection.execute(
                "UPDATE operations SET state='succeeded',updated_at_ms=? "
                "WHERE operation_id=? AND state='dispatched'",
                (now, operation_id))
            if updated.rowcount != 1:
                raise LedgerError("NOT_CALCULATION_DISPATCHED")
            self._event(connection, operation_id, "definitive_succeeded", now)
            return self._get(connection, operation_id)

    def read_calculation(self, operation_id: str, *, principal: str) -> int | None:
        """LEDGER-010: return result only to the same host-authenticated principal.

        None means there is no definitive saved result (old record, pending,
        failed, cancelled, or unknown). Never recompute based on missing output.
        """
        operation_id = _text("operation_id", operation_id)
        principal = _text("principal", principal)
        with self._open() as connection:
            record = connection.execute("""
                SELECT calculation_results.result
                FROM operations JOIN calculation_results USING(operation_id)
                WHERE operations.operation_id=?
                  AND operations.principal=?
                  AND operations.state='succeeded'
                  AND operations.tool_name='checked_add'
            """, (operation_id, principal)).fetchone()
        return None if record is None else int(record[0])

    def mark_unknown(self, operation_id: str) -> Operation:
        """Network failures after possible dispatch MUST become UNKNOWN."""
        operation_id = _text("operation_id", operation_id)
        now = self._now()
        with self._transaction() as connection:
            self._guard_legacy_write(connection, operation_id)
            op = self._get(connection, operation_id)
            if op.state != "dispatched":
                raise LedgerError("NOT_DISPATCHED")
            connection.execute("UPDATE operations SET state='unknown',updated_at_ms=? WHERE operation_id=?",
                               (now, operation_id))
            self._event(connection, operation_id, "outcome_unknown", now)
            return self._get(connection, operation_id)

    def recover_interrupted(self) -> int:
        """On app startup, conservatively quarantine all in-flight dispatches."""
        now = self._now()
        with self._transaction() as connection:
            has_leases = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operation_leases'"
            ).fetchone()
            query = "SELECT operation_id FROM operations WHERE state='dispatched'"
            if has_leases:
                query += " AND operation_id NOT IN (SELECT operation_id FROM operation_leases)"
            query += " ORDER BY operation_id"
            ids = [row[0] for row in connection.execute(query)]
            for operation_id in ids:
                connection.execute("UPDATE operations SET state='unknown',updated_at_ms=? WHERE operation_id=?",
                                   (now, operation_id))
                self._event(connection, operation_id, "recovered_as_unknown", now)
            return len(ids)

    def reconcile(self, operation_id: str, *, result: Literal["succeeded", "failed"],
                  evidence_reference: str) -> Operation:
        """Resolve UNKNOWN only with trusted external evidence (caller must verify)."""
        operation_id = _text("operation_id", operation_id)
        if result not in ("succeeded", "failed"):
            raise LedgerError("INVALID_RESULT")
        # Store only the hash, never the possibly sensitive external evidence.
        evidence_reference = _text("evidence_reference", evidence_reference)
        now = self._now()
        with self._transaction() as connection:
            op = self._get(connection, operation_id)
            if op.state != "unknown":
                raise LedgerError("NOT_UNKNOWN")
            connection.execute("UPDATE operations SET state=?,updated_at_ms=? WHERE operation_id=?",
                               (result, now, operation_id))
            self._event(connection, operation_id,
                        "reconciled_" + result + "_" + hashlib.sha256(
                            evidence_reference.encode("utf-8")).hexdigest(), now)
            return self._get(connection, operation_id)

    def history(self, operation_id: str) -> list[str]:
        with self._open() as connection:
            self._get(connection, _text("operation_id", operation_id))
            return [row[0] for row in connection.execute(
                "SELECT event_name FROM events WHERE operation_id=? ORDER BY sequence",
                (operation_id,))]
