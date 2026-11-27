"""task.operations.007: test records are bound to the hashes of what they checked, and stale evidence is not reused.

scripts/evidence_freshness.py classifies every task and gate record as fresh,
stale or unbound, fails --require-fresh on a stale record, and rebinds a gate
only after a passing live rerun. These tests change bound files and reports
and require the record to turn stale, a snapshot check of a stale gate to be
refused, and a failing rerun to leave the record exactly as it was.
"""
from __future__ import annotations

from hashlib import sha256
from importlib import util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = util.spec_from_file_location("evidence_freshness", ROOT / "scripts" / "evidence_freshness.py")
freshness = util.module_from_spec(spec)
spec.loader.exec_module(freshness)
import verify_phase  # noqa: E402


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "evidence" / "gates").mkdir(parents=True)
    (tmp_path / "src" / "a.py").write_text("A = 1\n")
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    report = tmp_path / "evidence" / "gates" / "demo-tests.xml"
    report.write_text("<testsuite tests='1'/>\n")
    record = {"task_id": "gate.demo.completed", "verification_status": "passed", "executed": True,
              "report_path": "evidence/gates/demo-tests.xml", "report_sha256": digest(report),
              "source_sha256": {"src/a.py": digest(tmp_path / "src" / "a.py")}}
    (tmp_path / "evidence" / "gates" / "demo.json").write_text(json.dumps(record))
    return tmp_path


def test_every_repository_record_is_classified_and_stale_ones_name_their_changes():
    summary = freshness.summary()
    assert summary["records"] == summary["fresh"] + summary["stale"] + summary["unbound"] >= 250
    assert all(row["changed"] for row in summary["stale_records"])


def test_a_changed_source_or_report_makes_the_record_stale(project):
    record = project / "evidence" / "gates" / "demo.json"
    assert freshness.freshness(record, project)["state"] == "fresh"
    (project / "src" / "a.py").write_text("A = 2\n")
    assert freshness.freshness(record, project) == {"record": "evidence/gates/demo.json",
                                                    "task_id": "gate.demo.completed", "state": "stale",
                                                    "changed": ["src/a.py"]}
    (project / "src" / "a.py").write_text("A = 1\n")
    (project / "evidence" / "gates" / "demo-tests.xml").write_text("<testsuite tests='2'/>\n")
    assert freshness.freshness(record, project)["changed"] == ["evidence/gates/demo-tests.xml"]


def test_a_snapshot_check_refuses_a_stale_gate(project, monkeypatch):
    monkeypatch.setattr(verify_phase, "ROOT", project)
    assert verify_phase.archived_report("demo") == project / "evidence/gates/demo-tests.xml"
    (project / "src" / "a.py").write_text("A = 2\n")
    with pytest.raises(ValueError, match="Snapshot differs"):
        verify_phase.archived_report("demo")


def test_require_fresh_fails_on_any_stale_record(tmp_path, capsys):
    fresh = tmp_path / "fresh.json"
    fresh.write_text(json.dumps({"task_id": "t", "source_sha256": {"pyproject.toml": digest(ROOT / "pyproject.toml")}}))
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps({"task_id": "t", "source_sha256": {"pyproject.toml": "0" * 64}}))
    assert freshness.main(["--require-fresh", str(fresh)]) == 0
    capsys.readouterr()
    assert freshness.main(["--require-fresh", str(fresh), str(stale)]) == 1
    assert [row["state"] for row in json.loads(capsys.readouterr().out)] == ["fresh", "stale"]


def test_a_passing_rerun_rebinds_the_gate_to_the_current_files(project):
    (project / "src" / "a.py").write_text("A = 2\n")

    def passing(phase, report, root):
        report.write_text("<testsuite tests='1' failures='0'/>\n")
        return 0

    result = freshness.refresh_gate("demo", project, runner=passing, files=lambda phase, root: {"tests/test_a.py"})
    record = json.loads((project / "evidence/gates/demo.json").read_text())
    assert result["state"] == "fresh"
    assert set(record["source_sha256"]) == {"src/a.py", "tests/test_a.py"}
    assert record["previous_report"]["sha256"] != record["report_sha256"] and record["refreshed_at"]
    assert record["report_path"] == "evidence/gates/demo-tests.xml"


def test_a_failing_rerun_leaves_the_gate_record_untouched(project):
    (project / "src" / "a.py").write_text("A = 2\n")
    before = {path: path.read_bytes() for path in (project / "evidence/gates").iterdir()}

    def failing(phase, report, root):
        report.write_text("<testsuite tests='1' failures='1'/>\n")
        return 1

    with pytest.raises(RuntimeError, match="was not changed"):
        freshness.refresh_gate("demo", project, runner=failing, files=lambda phase, root: set())
    assert {path: path.read_bytes() for path in (project / "evidence/gates").iterdir()} == before
    assert freshness.freshness(project / "evidence/gates/demo.json", project)["state"] == "stale"


def test_a_shared_report_is_never_overwritten_by_a_rerun(project):
    shared = project / "evidence" / "shared-tests.xml"
    shared.write_text("<testsuite tests='9'/>\n")
    record_path = project / "evidence/gates/demo.json"
    record = json.loads(record_path.read_text())
    record_path.write_text(json.dumps({**record, "report_path": "evidence/shared-tests.xml",
                                       "report_sha256": digest(shared)}))
    before = shared.read_bytes()

    def passing(phase, report, root):
        report.write_text("<testsuite tests='1' failures='0'/>\n")
        return 0

    freshness.refresh_gate("demo", project, runner=passing, files=lambda phase, root: set())
    assert shared.read_bytes() == before
    assert json.loads(record_path.read_text())["previous_report"]["path"] == "evidence/shared-tests.xml"


def test_records_bound_by_git_blob_ids_are_checked_too(project):
    blob_record = project / "evidence" / "gates" / "blobs.json"
    blob_record.write_text(json.dumps({"task_id": "gate.blobs.completed",
                                       "source_blobs": {"src/a.py": freshness.git_blob(project / "src/a.py")}}))
    assert freshness.freshness(blob_record, project)["state"] == "fresh"
    (project / "src" / "a.py").write_text("A = 3\n")
    assert freshness.freshness(blob_record, project)["changed"] == ["src/a.py"]
    assert freshness.git_blob(ROOT / "pyproject.toml") == __import__("subprocess").run(
        ["git", "hash-object", "pyproject.toml"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
