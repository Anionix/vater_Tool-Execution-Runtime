#!/usr/bin/env python3
"""gate.scope.completed: rebuild the scope gate's evidence on executed checks.

  python scripts/rebuild_scope_gate.py --rechecked README.md

The scope gate binds README.md and docs/SCOPE_CONTRACT.md (the purpose and boundary
wording, which a person judges) and requirements-py313.lock by git blob. When one of the two
documents changed since a person last re-checked it, the rebuild refuses until the run names
it with --rechecked: tests cannot judge whether the wording still states the scope. Then:

  1. the ledger: the 18 scope tasks are done and are exactly the gate's dependencies;
  2. every prerequisite record exists, names its task, passed and was executed;
  3. the dependency lock validates (33 packages, 485 artifact hashes, mcp 2.3.0);
  4. tests/test_scope_reconciliation.py re-checks each task's acceptance criterion against the
     current files, into evidence/gates/scope-tests.xml, with no failure and no skip;

and only then is evidence/gates/scope.json rewritten, bound to the current files and report,
with the previous binding kept. (scripts/evidence_freshness.py --refresh-gate does not apply:
most scope records predate the exact-case mapping that verify_phase.py needs.)
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_dependency_lock import validate_lock  # noqa: E402
from check_test_report import check_report  # noqa: E402
from evidence_freshness import git_blob  # noqa: E402

GATE = "evidence/gates/scope.json"
REPORT = "evidence/gates/scope-tests.xml"
TESTS = "tests/test_scope_reconciliation.py"
HUMAN_CHECKED = ("README.md", "docs/SCOPE_CONTRACT.md")
BLOB_BOUND = (*HUMAN_CHECKED, "requirements-py313.lock")
HASH_BOUND = (TESTS, "scripts/rebuild_scope_gate.py", "config/limits.json",
              "contracts/checked_add.output.contract.json", "pyproject.toml")
SDK_RECORD = "evidence/verified_tasks/task.scope.015.json"


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def needs_recheck(previous: dict, root: Path = ROOT) -> list[str]:
    """Human-checked files whose content differs from what a person last re-checked."""
    checked = (previous.get("human_recheck") or {}).get("blobs") or previous.get("source_blobs") or {}
    return [name for name in HUMAN_CHECKED if git_blob(root / name) != checked.get(name)]


def ledger_checks(root: Path = ROOT) -> tuple[dict, list[dict]]:
    rows = [json.loads(line) for line in (root / "backlog" / "tasks.jsonl").read_text(encoding="utf-8").splitlines()]
    tasks = {row["task_id"]: row for row in rows}
    gate = tasks["gate.scope.completed"]
    members = sorted(key for key, row in tasks.items() if row["phase"] == "scope" and key.startswith("task."))
    if members != sorted(gate["depends_on"]) or len(members) != 18:
        raise ValueError("the scope tasks are not exactly the gate's dependencies")
    if any(tasks[key]["status"] != "done" for key in members):
        raise ValueError("a scope task is not done")
    prerequisites = []
    for key in members:
        for relative in tasks[key]["evidence_paths"]:
            record = json.loads((root / relative).read_text(encoding="utf-8"))
            if (record.get("task_id") != key or record.get("verification_status") != "passed"
                    or record.get("executed") is not True):
                raise ValueError(f"evidence does not match its task: {relative}")
            prerequisites.append({"task_id": key, "path": relative})
    return gate, prerequisites


def lock_checks(root: Path = ROOT) -> dict:
    text = (root / "requirements-py313.lock").read_text(encoding="utf-8")
    packages = validate_lock(text)
    hashes = len(re.findall(r"--hash=sha256:", text))
    if len(packages) != 33 or hashes != 485 or packages.get("mcp") != "2.3.0":
        raise ValueError("the dependency lock is not the reviewed one")
    return {"dependency_packages": len(packages), "artifact_hashes": hashes}


def run_tests(root: Path = ROOT) -> dict:
    report = root / REPORT
    candidate = report.with_name(report.name + ".candidate")
    candidate.unlink(missing_ok=True)
    completed = subprocess.run([sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider",
                                f"--junitxml={candidate}", TESTS], cwd=root)
    if completed.returncode != 0 or not candidate.is_file():
        candidate.unlink(missing_ok=True)
        raise ValueError("the scope reconciliation tests did not pass; the gate record was not changed")
    totals = check_report(candidate, minimum=18)  # no failure, error or skip
    os.replace(candidate, report)
    return totals


def build(rechecked: list[str], date: str, root: Path = ROOT) -> dict:
    path = root / GATE
    previous = json.loads(path.read_text(encoding="utf-8"))
    missing = sorted(set(needs_recheck(previous, root)) - set(rechecked))
    if missing:
        raise ValueError("changed since a person last re-checked it; re-check, then pass --rechecked: "
                         + ", ".join(missing))
    gate, prerequisites = ledger_checks(root)
    lock = lock_checks(root)
    totals = run_tests(root)
    sdk = json.loads((root / SDK_RECORD).read_text(encoding="utf-8"))
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    record = {
        "task_id": "gate.scope.completed", "verification_status": "passed", "executed": True,
        "verification_date": date, "source_commit": head, "acceptance_criteria": gate["acceptance_criteria"],
        "checks": {"scope_task_count": 18, "tasks_done": 18, "all_evidence_records_read_and_matched": True,
                   "current_scope_reconciliation_checks_passed": totals["passed"],
                   "current_scope_reconciliation_checks_total": totals["passed"], **lock,
                   "official_mcp_integration_passed": sdk["official_sdk_tests"]["passed"],
                   "official_mcp_integration_skipped": sdk["official_sdk_tests"]["skipped"]},
        "prerequisite_evidence": prerequisites,
        "human_recheck": {
            "blobs": {name: git_blob(root / name) for name in HUMAN_CHECKED},
            "rechecked": sorted(rechecked), "date": date,
            "statement": ("A person re-checked the wording of the files named in 'rechecked' at these blobs before "
                          "this rebuild; the tests check each scope criterion mechanically.")},
        "source_blobs": {name: git_blob(root / name) for name in BLOB_BOUND},
        "source_sha256": {name: digest(root / name) for name in HASH_BOUND},
        "report_path": REPORT, "report_sha256": digest(root / REPORT),
        "integration_run": sdk["run_url"],
        "previous_binding": {key: previous.get(key) for key in ("source_commit", "verification_date", "source_blobs")},
        "limitation": ("Each scope task's acceptance criterion is re-checked against the current files by "
                       f"{TESTS}; the original archive's scope tests are not rerun. The wording of README.md and "
                       "docs/SCOPE_CONTRACT.md is judged by a person. Not a security proof or production approval."),
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rechecked", action="append", default=[], choices=HUMAN_CHECKED,
                        help="a file whose wording a person re-checked for this rebuild")
    parser.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    args = parser.parse_args(argv)
    try:
        record = build(args.rechecked, args.date)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(record["checks"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
