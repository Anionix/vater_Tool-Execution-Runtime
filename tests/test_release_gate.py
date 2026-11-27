"""task.operations.009: a serious check failure stops distribution.

scripts/release_gate.py runs every security check of a release and allows it
only if all passed; the release job builds and keeps artifacts only after the
gate and every verification job passed. These tests run the real offline
checks, make each check fail in turn (a planted secret, a scan of another
lock, any failing exit status) and require the release to be refused, and
check that the workflow publishes nothing after a failure.
"""
from __future__ import annotations

from importlib import util
import json
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = util.spec_from_file_location("release_gate", ROOT / "scripts" / "release_gate.py")
release_gate = util.module_from_spec(spec)
spec.loader.exec_module(release_gate)
OFFLINE = {name: arguments for name, arguments in release_gate.CHECKS.items() if name != "live_vulnerability_scan"}
WORKFLOW = (ROOT / ".github/workflows/python-verification.yml").read_text()


def test_the_real_offline_security_checks_pass_on_this_repository():
    report = release_gate.gate(OFFLINE)
    assert report["failed"] == [] and report["release_allowed"] is True, report


@pytest.mark.parametrize("failing", sorted(release_gate.CHECKS))
def test_any_failed_security_check_refuses_the_release_after_running_every_check(failing):
    ran = []

    def runner(arguments):
        ran.append(arguments)
        return 1 if arguments == release_gate.CHECKS[failing] else 0

    report = release_gate.gate(runner=runner)
    assert report["release_allowed"] is False and report["failed"] == [failing]
    assert len(ran) == len(release_gate.CHECKS)  # the report names every failure, not only the first


def test_a_planted_secret_refuses_the_release(tmp_path):
    (tmp_path / "evidence.json").write_text('{"token": "ghp_' + "R3l3aseGateP1anted0123456789abcdefghij" + '"}\n')
    report = release_gate.gate({"secret_audit": ["scripts/audit_logs.py", str(tmp_path)]})
    assert report["release_allowed"] is False and report["failed"] == ["secret_audit"]


def test_a_saved_scan_of_another_lock_refuses_the_release(tmp_path):
    record = json.loads((ROOT / "evidence/security/osv-scan.json").read_text())
    stale = tmp_path / "osv-scan.json"
    stale.write_text(json.dumps({**record, "lock_sha256": "0" * 64}))
    report = release_gate.gate({"saved_vulnerability_scan": ["scripts/scan_vulnerabilities.py", "--check-record",
                                                             str(stale)]})
    assert report["release_allowed"] is False


def test_no_checks_is_never_a_release(tmp_path, monkeypatch):
    assert release_gate.gate({})["release_allowed"] is False
    monkeypatch.setattr(release_gate, "gate", lambda: {"checks": {}, "failed": [], "release_allowed": False})
    assert release_gate.main(["--output", str(tmp_path / "gate.json")]) == 1
    assert json.loads((tmp_path / "gate.json").read_text())["release_allowed"] is False


def test_the_release_job_publishes_only_after_the_gate_and_every_verification_job():
    job = re.split(r"\n  (?=[A-Za-z0-9_-]+:\n)", WORKFLOW.split("  release-artifacts:\n", 1)[1], maxsplit=1)[0]
    jobs = set(re.findall(r"^  ([A-Za-z0-9_-]+):\n", WORKFLOW.split("\njobs:\n", 1)[1], flags=re.M))
    needs = set(re.search(r"needs: \[([^\]]+)\]", job).group(1).replace(" ", "").split(","))
    assert needs == jobs - {"release-artifacts"}
    gate = job.index("python scripts/release_gate.py --output release-gate.json")
    assert gate < job.index("release_manifest.py") < job.index("name: release-artifacts")
    publish = job.split("- name: Keep the artifacts and their manifest", 1)[1].split("- name:", 1)[0]
    assert "if: always()" not in publish and "if-no-files-found: error" in publish
    report = job.split("- name: Keep the release gate report", 1)[1]
    assert "if: always()" in report and "release-gate.json" in report
