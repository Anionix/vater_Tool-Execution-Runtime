"""Real child process -> validation -> authorization -> SQLite -> calculation."""
import json
import os
from pathlib import Path
import subprocess
import sys

from verified_execution.ledger import OperationLedger

ROOT = Path(__file__).resolve().parents[1]


def launch(db, payload: bytes):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run([sys.executable, "-m", "verified_execution.recorded_cli",
                           "--database", str(db)],
                          cwd=ROOT, input=payload, capture_output=True,
                          timeout=10, env=environment)


def test_real_process_persists_and_prevents_replay(tmp_path):
    database = tmp_path / "operations.sqlite3"
    request = b'{"request_key":"run-1","name":"checked_add","arguments":{"left":5,"right":7}}\n'
    first = launch(database, request)
    assert first.returncode == 0, first.stderr
    outcome = json.loads(first.stdout)
    assert outcome["ok"] and outcome["value"] == 12
    assert outcome["operation_id"]
    second = launch(database, request)
    assert second.returncode == 0, second.stderr
    replay = json.loads(second.stdout)
    assert replay["ok"] and replay["value"] == 12 and replay["replayed"]
    assert outcome["operation_id"] == replay["operation_id"]
    assert OperationLedger(database).history(outcome["operation_id"]).count("dispatched") == 1


def test_real_process_rejects_duplicate_json_fields(tmp_path):
    database = tmp_path / "operations.sqlite3"
    payload = b'{"request_key":"one","request_key":"two","name":"checked_add","arguments":{"left":1,"right":2}}\n'
    completed = launch(database, payload)
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["error_code"] == "INVALID_MESSAGE"


def test_real_process_unauthorized_client_cannot_supply_identity(tmp_path):
    database = tmp_path / "operations.sqlite3"
    payload = b'{"request_key":"one","principal":"fake-admin","name":"checked_add","arguments":{"left":1,"right":2}}\n'
    completed = launch(database, payload)
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["error_code"] == "INVALID_MESSAGE"


def test_real_process_invalid_then_valid_recovers(tmp_path):
    database = tmp_path / "operations.sqlite3"
    payload = (b'{"request_key":"oops","name":"checked_add","arguments":{"left":1,}}\n'
               b'{"request_key":"okay","name":"checked_add","arguments":{"left":3,"right":4}}\n')
    completed = launch(database, payload)
    assert completed.returncode == 0
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [line["error_code"] for line in responses] == ["INVALID_MESSAGE", None]
    assert responses[1]["value"] == 7


def test_real_process_large_request_is_rejected_and_recovers(tmp_path):
    database = tmp_path / "operations.sqlite3"
    large = b'X' * 6000 + b'\n'
    okay = b'{"request_key":"okay","name":"checked_add","arguments":{"left":1,"right":1}}\n'
    completed = launch(database, large + okay)
    assert completed.returncode == 0
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert responses[0]["error_code"] == "INVALID_MESSAGE"
    assert responses[1]["value"] == 2
