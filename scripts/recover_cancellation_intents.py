"""Trusted local operator helper: never accepts client-supplied identity or tokens.

Run when writes to the SQLite database are functioning again. Exit statuses:
  0: all persisted intents have reached a safe terminal/quarantined state
  2: some are still pending (live owner or unknown operation)
  3: DB/storage failure or corrupted intent; needs human inspection
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sqlite3

from verified_execution.cancellation_journal import CancellationJournal, JournalError
from verified_execution.ledger import LedgerError
from verified_execution.worker_leases import LeasedLedger


def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument('--database', required=True)
    parser.add_argument('--journal', default=None)
    args=parser.parse_args()
    try:
        ledger=LeasedLedger(args.database, timeout_seconds=.5)
        journal=CancellationJournal(args.journal or str(Path(args.database).with_name(Path(args.database).name+'.cancel-intents')))
        result=journal.replay(ledger)
    except (sqlite3.Error, LedgerError, JournalError, OSError) as exc:
        # Avoid leaking SQL contents or file paths to the tool caller.
        print(json.dumps({'status':'error','category': type(exc).__name__},sort_keys=True))
        return 3
    pending=result['pending_owner']+result['not_found']
    result['status']='pending' if pending else 'recovered'
    print(json.dumps(result,sort_keys=True))
    return 2 if pending else 0


if __name__=='__main__':
    raise SystemExit(main())
