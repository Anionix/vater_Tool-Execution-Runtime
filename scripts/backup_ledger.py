#!/usr/bin/env python3
"""task.operations.016: back up and restore the ledger without executing anything twice.

  python scripts/backup_ledger.py backup  LEDGER BACKUP
  python scripts/backup_ledger.py restore BACKUP LEDGER [--replace]
  python scripts/backup_ledger.py reconcile LEDGER          # needs GITHUB_REPOSITORY and GITHUB_TOKEN
  python scripts/backup_ledger.py clear-fence LEDGER --reconciled-by "who checked what"

backup   copies a consistent snapshot with SQLite's online backup API (safe
         while the service writes), checks its integrity and writes
         BACKUP.manifest.json with its SHA-256 and the time it was started.
restore  verifies the manifest hash and integrity, never overwrites a ledger
         without --replace (the old file is kept aside), turns operations that
         were in flight at backup time into 'unknown' (settled by lookup, never
         resent) and installs a restore fence: executors with external effects
         send nothing new until the gap after the backup is reconciled.
reconcile lists destination objects updated since the backup; if the listing
         is complete and every object's operation is in the restored ledger,
         nothing ran in the gap and the fence is cleared. Otherwise a person
         reconciles the listed objects and runs clear-fence.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from verified_execution import restore_fence  # noqa: E402
from verified_execution.external_change import LookupIncomplete  # noqa: E402
from verified_execution.ledger import OperationLedger  # noqa: E402

GAP_CLEARED = "gap report: every destination object since the backup belongs to a restored operation"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def manifest_path(backup: Path) -> Path:
    return Path(f"{backup}.manifest.json")


def read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)


def integrity(path: Path) -> str:
    with closing(read_only(path)) as connection:
        return connection.execute("PRAGMA integrity_check").fetchone()[0]


def operations_by_state(path: Path) -> dict[str, int]:
    with closing(read_only(path)) as connection:
        return dict(connection.execute("SELECT state, COUNT(*) FROM operations GROUP BY state ORDER BY state"))


def backup(ledger: Path, target: Path) -> dict:
    taken_at_ms = int(time.time() * 1000)  # before the copy: the gap starts no later than this
    partial = target.with_name(target.name + ".partial")
    partial.unlink(missing_ok=True)
    with closing(read_only(ledger)) as source, closing(sqlite3.connect(partial)) as copy:
        source.backup(copy)
    checked = integrity(partial)
    if checked != "ok":
        partial.unlink()
        raise RuntimeError(f"backup failed its integrity check: {checked}")
    os.replace(partial, target)
    manifest = {"ledger": str(ledger), "backup": str(target), "taken_at_ms": taken_at_ms,
                "sha256": sha256_file(target), "integrity": checked,
                "operations_by_state": operations_by_state(target)}
    manifest_path(target).write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest


def restore(source: Path, ledger: Path, *, replace: bool = False) -> dict:
    manifest = json.loads(manifest_path(source).read_text(encoding="utf-8"))
    if sha256_file(source) != manifest["sha256"]:
        raise RuntimeError("the backup does not match its manifest")
    if integrity(source) != "ok":
        raise RuntimeError("the backup failed its integrity check")
    kept_aside = None
    if ledger.exists():
        if not replace:
            raise FileExistsError(f"{ledger} exists; pass --replace to set it aside")
        kept_aside = ledger.with_name(f"{ledger.name}.replaced-{int(time.time() * 1000)}")
        os.replace(ledger, kept_aside)
    partial = ledger.with_name(ledger.name + ".partial")
    partial.unlink(missing_ok=True)
    with closing(read_only(source)) as backup_db, closing(sqlite3.connect(partial)) as copy:
        backup_db.backup(copy)
    os.replace(partial, ledger)
    # In flight when the backup was taken: the outcome is unknown, settle by lookup only.
    quarantined = OperationLedger(ledger).recover_interrupted()
    fence = restore_fence.install(ledger, backup_taken_at_ms=manifest["taken_at_ms"],
                                  backup_sha256=manifest["sha256"])
    return {"restored": str(ledger), "kept_aside": kept_aside and str(kept_aside),
            "backup_taken_at_ms": manifest["taken_at_ms"], "in_flight_now_unknown": quarantined,
            "fence_id": fence, "operations_by_state": operations_by_state(ledger)}


def gap_report(ledger: Path, destination) -> dict:
    fence = restore_fence.active(ledger)
    if fence is None:
        return {"fence": None}
    with closing(read_only(ledger)) as connection:
        known = {row[0] for row in connection.execute("SELECT operation_id FROM operations")}
    try:
        seen = destination.operation_ids_since(fence["backup_taken_at_ms"])
    except LookupIncomplete as error:
        return {"fence": fence, "complete": False, "detail": str(error)}
    return {"fence": fence, "complete": True, "destination_objects": len(seen),
            "missing_from_ledger": sorted(seen - known)}


def reconcile(ledger: Path, destination) -> dict:
    report = gap_report(ledger, destination)
    if report.get("complete") and not report["missing_from_ledger"]:
        report["cleared"] = restore_fence.clear(ledger, reconciled_by=GAP_CLEARED)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    making = sub.add_parser("backup")
    making.add_argument("ledger", type=Path)
    making.add_argument("backup", type=Path)
    restoring = sub.add_parser("restore")
    restoring.add_argument("backup", type=Path)
    restoring.add_argument("ledger", type=Path)
    restoring.add_argument("--replace", action="store_true")
    reconciling = sub.add_parser("reconcile")
    reconciling.add_argument("ledger", type=Path)
    clearing = sub.add_parser("clear-fence")
    clearing.add_argument("ledger", type=Path)
    clearing.add_argument("--reconciled-by", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "backup":
            result = backup(args.ledger, args.backup)
        elif args.action == "restore":
            result = restore(args.backup, args.ledger, replace=args.replace)
        elif args.action == "reconcile":
            from verified_execution.external_change import GitHubIssueDestination, UrllibTransport
            destination = GitHubIssueDestination(UrllibTransport(os.environ["GITHUB_REPOSITORY"],
                                                                 os.environ["GITHUB_TOKEN"]))
            result = reconcile(args.ledger, destination)
        else:
            result = {"cleared": restore_fence.clear(args.ledger, reconciled_by=args.reconciled_by)}
    except (OSError, RuntimeError, ValueError, KeyError, sqlite3.Error) as error:
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1))
    return 0 if args.action != "reconcile" or result.get("cleared") else 2


if __name__ == "__main__":
    sys.exit(main())
