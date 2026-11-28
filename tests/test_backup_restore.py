"""task.operations.016: backup and restore of stored data, with no duplicate execution after a restore.

scripts/backup_ledger.py takes consistent online backups of the ledger (with
the external result store in the same file) and restores them. These tests
lose the ledger and restore it, then resubmit requests: a recorded operation
is replayed, an operation in flight at backup time is settled by lookup, and
an operation executed after the backup is not executed again, because the
restore fence stops new external effects until the gap is reconciled.
"""
from __future__ import annotations

from importlib import util
import itertools
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from test_external_change import Crash, FakeGitHub, Host
from verified_execution import restore_fence
from verified_execution.external_change import GitHubIssueDestination
from verified_execution.ledger import OperationLedger

ROOT = Path(__file__).resolve().parents[1]
spec = util.spec_from_file_location("backup_ledger", ROOT / "scripts" / "backup_ledger.py")
backups = util.module_from_spec(spec)
spec.loader.exec_module(backups)


HOSTS = itertools.count(1)


def host(ledger: Path, github: FakeGitHub) -> Host:
    """A host process; each run issues approval ids of its own, as a restarted host would."""
    started = Host(ledger, github)
    started.approvals = next(HOSTS) * 1000
    return started


def lose_and_restore(ledger: Path, backup: Path) -> dict:
    ledger.unlink()  # the disk holding the ledger is gone
    return backups.restore(backup, ledger)


def only_operation(ledger: Path) -> str:
    with sqlite3.connect(ledger) as connection:
        (operation_id,), = connection.execute("SELECT operation_id FROM operations").fetchall()
    return operation_id


def test_a_recorded_operation_is_replayed_after_a_restore_not_executed(tmp_path):
    github, ledger, backup = FakeGitHub(), tmp_path / "ledger.sqlite3", tmp_path / "backup.sqlite3"
    first = host(ledger, github).create("k-1")
    assert first.ok and github.posts == 1
    manifest = backups.backup(ledger, backup)
    assert manifest["operations_by_state"] == {"succeeded": 1} and manifest["integrity"] == "ok"
    report = lose_and_restore(ledger, backup)
    assert (report["in_flight_now_unknown"], report["operations_by_state"]) == (0, {"succeeded": 1})
    again = host(ledger, github).create("k-1")
    assert (again.ok, again.replayed, again.value) == (True, True, first.value)
    assert github.posts == 1 and len(github.issues) == 1


def test_an_operation_in_flight_at_backup_time_is_settled_by_lookup_never_resent(tmp_path):
    github, ledger, backup = FakeGitHub(), tmp_path / "ledger.sqlite3", tmp_path / "backup.sqlite3"
    github.script = ["crash_after_create"]
    with pytest.raises(Crash):
        host(ledger, github).create("k-1")
    assert OperationLedger(ledger).get(only_operation(ledger)).state == "dispatched"
    backups.backup(ledger, backup)
    report = lose_and_restore(ledger, backup)
    assert report["in_flight_now_unknown"] == 1
    replay = host(ledger, github).create("k-1")
    assert (replay.ok, replay.value) == (True, 1)  # found by its marker, under the fence
    assert github.posts == 1 and len(github.issues) == 1


def test_an_operation_executed_after_the_backup_is_not_executed_again(tmp_path):
    github, ledger, backup = FakeGitHub(), tmp_path / "ledger.sqlite3", tmp_path / "backup.sqlite3"
    host(ledger, github).create("k-1")
    backups.backup(ledger, backup)
    after_backup = host(ledger, github).create("k-2")  # executed, then the disk is lost
    assert after_backup.ok and github.posts == 2
    lose_and_restore(ledger, backup)
    resubmitted = host(ledger, github).create("k-2")
    assert (resubmitted.ok, resubmitted.error_code) == (False, "HUMAN_RECONCILIATION_REQUIRED")
    assert github.posts == 2 and len(github.issues) == 2  # no second issue for k-2
    report = backups.reconcile(ledger, GitHubIssueDestination(github))
    assert report["complete"] and report["missing_from_ledger"] == [after_backup.operation_id]
    assert "cleared" not in report and restore_fence.active(ledger) is not None
    with pytest.raises(ValueError):
        restore_fence.clear(ledger, reconciled_by=" ")
    # A person matched the listed issue to request k-2 and does not resubmit it.
    assert restore_fence.clear(ledger, reconciled_by="operator: issue #2 is request k-2") == 1
    assert host(ledger, github).create("k-3").ok and github.posts == 3


def test_a_gap_without_destination_activity_is_cleared_by_the_report(tmp_path):
    github, ledger, backup = FakeGitHub(), tmp_path / "ledger.sqlite3", tmp_path / "backup.sqlite3"
    host(ledger, github).create("k-1")
    backups.backup(ledger, backup)
    lose_and_restore(ledger, backup)
    assert host(ledger, github).create("k-2").error_code == "HUMAN_RECONCILIATION_REQUIRED"
    report = backups.reconcile(ledger, GitHubIssueDestination(github))
    assert (report["missing_from_ledger"], report["cleared"]) == ([], 1)
    assert restore_fence.active(ledger) is None
    assert host(ledger, github).create("k-2").ok and github.posts == 2  # the held request proceeds once


def test_an_incomplete_destination_listing_keeps_the_fence(tmp_path):
    github, ledger, backup = FakeGitHub(), tmp_path / "ledger.sqlite3", tmp_path / "backup.sqlite3"
    OperationLedger(ledger)
    backups.backup(ledger, backup)
    lose_and_restore(ledger, backup)
    github.lookup_fails = True
    report = backups.reconcile(ledger, GitHubIssueDestination(github))
    assert report["complete"] is False and "cleared" not in report
    assert restore_fence.active(ledger)["backup_sha256"] == json.loads(
        Path(f"{backup}.manifest.json").read_text())["sha256"]


def test_restore_refuses_a_damaged_backup_and_never_overwrites_without_replace(tmp_path):
    ledger, backup = tmp_path / "ledger.sqlite3", tmp_path / "backup.sqlite3"
    OperationLedger(ledger)
    backups.backup(ledger, backup)
    with pytest.raises(FileExistsError):
        backups.restore(backup, ledger)
    report = backups.restore(backup, ledger, replace=True)
    assert Path(report["kept_aside"]).is_file()
    damaged = bytearray(backup.read_bytes())
    damaged[-1] ^= 0xFF
    backup.write_bytes(bytes(damaged))
    target = tmp_path / "other.sqlite3"
    with pytest.raises(RuntimeError, match="manifest"):
        backups.restore(backup, target)
    assert not target.exists()


def test_a_backup_taken_while_the_service_writes_is_consistent(tmp_path):
    ledger, backup = tmp_path / "ledger.sqlite3", tmp_path / "backup.sqlite3"
    writer_ledger = OperationLedger(ledger)
    stop = threading.Event()

    def write():
        n = 0
        while not stop.is_set() and n < 400:
            n += 1
            op = writer_ledger.reserve(principal="p", request_key=f"k-{n}", tool_name="checked_add_recorded",
                                       target_id=None, resource_version=None, arguments={"n": n},
                                       requires_approval=False)
            writer_ledger.begin_dispatch(op.operation_id)
            writer_ledger.record_definitive_result(op.operation_id, "succeeded")

    thread = threading.Thread(target=write)
    thread.start()
    try:
        manifest = backups.backup(ledger, backup)
    finally:
        stop.set()
        thread.join()
    assert manifest["integrity"] == "ok"
    states = manifest["operations_by_state"]
    # Each transaction is copied whole: at most the one operation between its transactions is open.
    assert set(states) <= {"prepared", "dispatched", "succeeded"}
    assert states.get("prepared", 0) + states.get("dispatched", 0) <= 1


def test_the_command_line_backs_up_restores_and_clears(tmp_path, capsys):
    ledger, backup = tmp_path / "ledger.sqlite3", tmp_path / "backup.sqlite3"
    OperationLedger(ledger)
    assert backups.main(["backup", str(ledger), str(backup)]) == 0
    assert backups.main(["restore", str(backup), str(ledger)]) == 1  # exists, no --replace
    assert backups.main(["restore", str(backup), str(ledger), "--replace"]) == 0
    assert restore_fence.active(ledger) is not None
    capsys.readouterr()
    assert backups.main(["clear-fence", str(ledger), "--reconciled-by", "operator"]) == 0
    assert json.loads(capsys.readouterr().out) == {"cleared": 1}
    assert restore_fence.active(ledger) is None


def test_a_fence_cannot_be_installed_where_no_ledger_can_be_written(tmp_path):
    with pytest.raises(sqlite3.Error):
        restore_fence.install(tmp_path, backup_taken_at_ms=1, backup_sha256="0" * 64)  # a directory
    assert restore_fence.active(tmp_path / "never-restored.sqlite3") is None
