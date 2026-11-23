"""Crash-resilient cancellation intents for a single trusted local host.

STATE MACHINE (CANCEL-DURABLE):
  UNRECORDED -> FSYNCED_PENDING -> {APPLIED | ALREADY_TERMINAL | PENDING_OWNER}
  PENDING_OWNER -> {EXPIRED_UNKNOWN | ALREADY_TERMINAL}
  SQLITE_BUSY -> leave FSYNCED_PENDING; NEVER falsely report APPLIED.

A journal intent is durable only after its file and directory fsync succeed.
No client can submit an owner token through this interface. Stale post-dispatch
operations are recovered by existing fenced lease rules, not force-cancelled.
A crash/power loss during fsync may still require manual storage inspection.
This is local POSIX storage, not authenticated remote storage or a rollback.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any
from uuid import uuid4

from .ledger import LedgerError, _text
from .worker_leases import LeasedLedger


class JournalError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class CancellationJournal:
    """One atomic, owner-scoped JSON file per cancellation intent."""

    def __init__(self, path: str | Path) -> None:
        if os.name != 'posix':
            raise JournalError('POSIX_STORAGE_REQUIRED')
        self.directory = Path(path)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise JournalError('UNTRUSTED_JOURNAL_DIRECTORY')

    @staticmethod
    def _file_name(operation_id: str) -> str:
        return hashlib.sha256(operation_id.encode('utf-8')).hexdigest() + '.json'

    def _sync_directory(self) -> None:
        fd = os.open(self.directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def persist(self, operation_id: str, *, principal: str) -> None:
        """Record intent BEFORE attempting a potentially busy SQLite transaction."""
        operation_id = _text('operation_id', operation_id)
        principal = _text('principal', principal)
        target = self.directory / self._file_name(operation_id)
        record = {'schema_version': 1, 'operation_id': operation_id, 'principal': principal,
                  'intent': 'cancel'}
        payload = (json.dumps(record, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
        if target.exists():
            old = self._read(target)
            if old != record:
                raise JournalError('INTENT_CONFLICT')
            return
        temp = self.directory / ('.writing-' + str(uuid4()))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
        try:
            fd = os.open(temp, flags, 0o600)
            try:
                with os.fdopen(fd, 'wb', closefd=True) as out:
                    out.write(payload)
                    out.flush()
                    os.fsync(out.fileno())
            except BaseException:
                raise
            os.replace(temp, target)
            self._sync_directory()
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            meta = path.lstat()
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
                raise JournalError('UNTRUSTED_INTENT_FILE')
            if meta.st_size > 2048:
                raise JournalError('CORRUPTED_INTENT_FILE')
            def no_duplicates(pairs):
                out = {}
                for key, value in pairs:
                    if key in out:
                        raise JournalError('CORRUPTED_INTENT_FILE')
                    out[key] = value
                return out
            data = json.loads(path.read_bytes(), object_pairs_hook=no_duplicates)
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise JournalError('CORRUPTED_INTENT_FILE') from exc
        if (type(data) is not dict or set(data) != {'schema_version','operation_id','principal','intent'}
                or data['schema_version'] != 1 or data['intent'] != 'cancel'
                or type(data['operation_id']) is not str or type(data['principal']) is not str
                or self._file_name(data['operation_id']) != path.name):
            raise JournalError('CORRUPTED_INTENT_FILE')
        return data

    def list_intents(self) -> list[dict[str, Any]]:
        entries = []
        for file in sorted(self.directory.glob('*.json')):
            entries.append(self._read(file))
        return entries

    def _clear(self, operation_id: str) -> None:
        (self.directory / self._file_name(operation_id)).unlink(missing_ok=True)
        self._sync_directory()

    def replay(self, ledger: LeasedLedger) -> dict[str, int]:
        """Idempotent, conservative recovery. Throws on database failures.

        Results never claim cancellation of dispatched work. An unexpired
        owner remains protected, and the intent stays pending for later replay.
        """
        counters = {'applied_cancelled': 0, 'already_terminal': 0,
                    'expired_unknown': 0, 'pending_owner': 0, 'not_found': 0}
        for intent in self.list_intents():
            operation_id = intent['operation_id']
            try:
                op = ledger.get(operation_id)
            except LedgerError as error:
                if error.code == 'UNKNOWN_OPERATION':
                    counters['not_found'] += 1
                    continue
                raise
            if op.principal != intent['principal']:
                raise JournalError('PRINCIPAL_MISMATCH')
            if op.state == 'prepared':
                after = ledger.request_cancel(operation_id)
                if after.state != 'cancelled':
                    raise JournalError('UNEXPECTED_CANCEL_STATE')
                counters['applied_cancelled'] += 1
                self._clear(operation_id)
            elif op.state == 'dispatched':
                # Never force another worker's active dispatch to unknown.
                ledger.recover_expired()
                after = ledger.get(operation_id)
                if after.state == 'dispatched':
                    counters['pending_owner'] += 1
                elif after.state == 'unknown':
                    counters['expired_unknown'] += 1
                    self._clear(operation_id)
                elif after.state in ('succeeded','failed','cancelled'):
                    counters['already_terminal'] += 1
                    self._clear(operation_id)
                else:
                    raise JournalError('UNEXPECTED_CANCEL_STATE')
            elif op.state in ('succeeded','failed','unknown','cancelled'):
                counters['already_terminal'] += 1
                self._clear(operation_id)
            else:
                raise JournalError('UNEXPECTED_CANCEL_STATE')
        return counters
