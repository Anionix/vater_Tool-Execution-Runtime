"""gate.scope.completed: the rebuild needs a person's re-check of changed wording, and its record holds.

scripts/rebuild_scope_gate.py rewrites the scope gate only after the README or the scope
contract, when changed, were re-checked by a person (--rechecked), and after the ledger, the
prerequisite records, the dependency lock and the per-task reconciliation tests all pass.
"""
from __future__ import annotations

from importlib import util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load(name: str):
    spec = util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rebuild = load("rebuild_scope_gate")
freshness = load("evidence_freshness")
RECORD = json.loads((ROOT / "evidence" / "gates" / "scope.json").read_text(encoding="utf-8"))


def test_a_changed_document_needs_a_stated_recheck_before_anything_runs(tmp_path, monkeypatch):
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("changed wording\n", encoding="utf-8")
    (tmp_path / "docs" / "SCOPE_CONTRACT.md").write_text((ROOT / "docs" / "SCOPE_CONTRACT.md").read_text(
        encoding="utf-8"), encoding="utf-8")
    (tmp_path / "evidence" / "gates").mkdir(parents=True)
    (tmp_path / rebuild.GATE).write_text(json.dumps(RECORD), encoding="utf-8")
    ran = []
    monkeypatch.setattr(rebuild, "run_tests", lambda root: ran.append(root))
    assert rebuild.needs_recheck(RECORD, tmp_path) == ["README.md"]
    with pytest.raises(ValueError, match="README.md"):
        rebuild.build([], "2026-11-28", tmp_path)
    assert ran == [] and json.loads((tmp_path / rebuild.GATE).read_text()) == RECORD  # nothing ran or changed


def test_an_older_record_without_a_recheck_falls_back_to_its_bound_blobs():
    older = {"source_blobs": {"README.md": "0" * 40, "docs/SCOPE_CONTRACT.md": RECORD["human_recheck"]["blobs"][
        "docs/SCOPE_CONTRACT.md"]}}
    assert rebuild.needs_recheck(older) == ["README.md"]
    assert rebuild.needs_recheck(RECORD) == []  # the current wording is what a person re-checked


def test_the_rebuilt_record_is_fresh_and_matches_the_ledger_and_its_report():
    state = freshness.freshness(ROOT / "evidence" / "gates" / "scope.json", ROOT)
    assert state["state"] == "fresh", state
    assert RECORD["human_recheck"]["blobs"] == {name: freshness.git_blob(ROOT / name) for name in rebuild.HUMAN_CHECKED}
    assert RECORD["human_recheck"]["rechecked"] and set(RECORD["human_recheck"]["rechecked"]) <= set(rebuild.HUMAN_CHECKED)
    _, prerequisites = rebuild.ledger_checks()
    assert RECORD["prerequisite_evidence"] == prerequisites and len({p["task_id"] for p in prerequisites}) == 18
    totals = load("check_test_report").check_report(ROOT / RECORD["report_path"], minimum=18)
    assert totals["passed"] == RECORD["checks"]["current_scope_reconciliation_checks_passed"] == 18
    assert RECORD["checks"]["dependency_packages"] == 33 and RECORD["checks"]["artifact_hashes"] == 485
    assert RECORD["previous_binding"]["source_blobs"]["README.md"] != RECORD["source_blobs"]["README.md"]
