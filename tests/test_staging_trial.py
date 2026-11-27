"""task.operations.010: a small staging trial runs first, without production data.

scripts/staging_trial.py runs the real MCP server over stdio, the external
tool against a synthetic destination, the kill switch, backup and restore and
the monitor, all inside one new staging directory. These tests require every
check to pass, every file to stay inside the staging directory, no credential
value to reach the trial, and a host ledger outside the staging directory
(real data) to be refused untouched.
"""
from __future__ import annotations

from hashlib import sha256
from importlib import util
from pathlib import Path
import subprocess
import sys

import pytest

from verified_execution.ledger import OperationLedger

ROOT = Path(__file__).resolve().parents[1]
spec = util.spec_from_file_location("staging_trial", ROOT / "scripts" / "staging_trial.py")
staging_trial = util.module_from_spec(spec)
sys.modules[spec.name] = staging_trial
spec.loader.exec_module(staging_trial)
PLANTED = "ghp_" + "StagingPlantedValue0123456789abcdefghij"


def repository_state() -> str:
    return subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT,
                          capture_output=True, text=True, check=True).stdout


def test_the_trial_passes_inside_its_staging_directory_only(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    monkeypatch.setenv("HOME", str(tmp_path / "operator-home"))
    monkeypatch.setenv("GITHUB_TOKEN", PLANTED)
    monkeypatch.delenv(staging_trial.LEDGER_VARIABLE, raising=False)
    before = repository_state()
    staging = tmp_path / "staging"
    staging.mkdir()
    report = staging_trial.run(staging)
    assert report["passed"] is True, report["checks"]
    assert set(report["checks"]) >= {"mcp_recorded_replay_without_dispatch", "kill_switch_stops",
                                     "restore_replays_without_resend", "monitor_quiet"}
    assert report["data"] == "synthetic only" and "GITHUB_TOKEN" in report["credentials_withheld"]
    assert report["files"] == ["external-ledger.backup.sqlite3", "external-ledger.backup.sqlite3.manifest.json",
                               "external-ledger.sqlite3", "kill-switch", "mcp-ledger.sqlite3",
                               "restored-ledger.sqlite3"]
    assert repository_state() == before  # nothing written into the repository
    assert not (tmp_path / "operator-home").exists()  # nor into the operator's home
    for path in staging.rglob("*"):
        if path.is_file():
            assert PLANTED.encode() not in path.read_bytes(), path


def test_a_ledger_outside_the_staging_directory_is_refused_untouched(tmp_path):
    production = tmp_path / "production.sqlite3"
    ledger = OperationLedger(production)
    ledger.reserve(principal="customer", request_key="real-1", tool_name="checked_add_recorded",
                   target_id=None, resource_version=None, arguments={"n": 1}, requires_approval=False)
    before = sha256(production.read_bytes()).hexdigest()
    staging = tmp_path / "staging"
    staging.mkdir()
    report = staging_trial.run(staging, {staging_trial.LEDGER_VARIABLE: str(production)})
    assert report["passed"] is False and report["refused"]
    assert sha256(production.read_bytes()).hexdigest() == before
    assert list(staging.iterdir()) == []  # nothing ran


def test_a_ledger_inside_the_staging_directory_is_allowed(tmp_path):
    assert staging_trial.preflight(tmp_path, {staging_trial.LEDGER_VARIABLE: str(tmp_path / "x.sqlite3")}) == []
    assert staging_trial.preflight(tmp_path, {}) == []


def test_ci_runs_the_staging_trial_in_the_locked_environment():
    workflow = (ROOT / ".github/workflows/python-verification.yml").read_text()
    trial = workflow.index("python scripts/staging_trial.py --output staging-trial.json")
    assert workflow.index("--require-hashes -r requirements-py313.lock") < trial
