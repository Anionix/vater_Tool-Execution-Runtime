#!/usr/bin/env python3
"""Verify a phase against exact pytest cases in its task evidence.

Live mode runs each required case once. Report mode checks recorded results.
The phase gate and each prerequisite are read from the existing task ledger.
"""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from xml.etree import ElementTree

from check_test_report import check_report

ROOT = Path(__file__).resolve().parents[1]


def phase_cases(phase: str) -> dict[str, list[str]]:
    rows = [json.loads(line) for line in (ROOT / "backlog/tasks.jsonl").read_text().splitlines()]
    tasks = {row["task_id"]: row for row in rows}
    if len(rows) != len(tasks):
        raise ValueError("Duplicate task identifier")
    gate = tasks.get(f"gate.{phase}.completed")
    members = {key for key, row in tasks.items() if row["phase"] == phase and key.startswith("task.")}
    if not gate or not members or members != set(gate["depends_on"]):
        raise ValueError(f"Phase membership does not match gate dependencies: {phase}")
    inventory = {}
    for key in sorted(members):
        row = tasks[key]
        if any(tasks[parent]["status"] != "done" for parent in row["depends_on"]):
            raise ValueError(f"Open prerequisite: {key}")
        records = []
        for relative in row["evidence_paths"]:
            path = (ROOT / relative).resolve()
            if not path.is_relative_to(ROOT / "evidence"):
                raise ValueError(f"Invalid evidence path: {key}")
            record = json.loads(path.read_text())
            if "test_cases" in record:
                records.append(record)
        if len(records) != 1:
            raise ValueError(f"Expected one case mapping: {key}")
        record = records[0]
        cases = record["test_cases"]
        if (record["task_id"] != key or record["acceptance_criteria"] != row["acceptance_criteria"]
                or record["executed"] is not True or record["verification_status"] != "passed"
                or not isinstance(cases, list) or not cases or not all(isinstance(node, str) for node in cases)
                or len(set(cases)) != len(cases)):
            raise ValueError(f"Evidence does not match acceptance criteria: {key}")
        for node in cases:
            file, sep, test = node.partition("::")
            path = (ROOT / file).resolve()
            if (not sep or not test or not file.startswith("tests/")
                    or not path.is_relative_to(ROOT / "tests") or not path.is_file()):
                raise ValueError(f"Unresolvable test case for {key}: {node}")
        inventory[key] = cases
    return inventory


def verify_report(report: Path, inventory: dict[str, list[str]]) -> dict:
    required = {node for cases in inventory.values() for node in cases}
    totals = check_report(report, minimum=len(required))
    observed = Counter((case.get("classname"), case.get("name"))
                       for case in ElementTree.parse(report).iter("testcase"))
    for node in sorted(required):
        definition, parameter, suffix = node.partition("[")
        file, *classes, function = definition.split("::")
        classname = ".".join([file.removesuffix(".py").replace("/", "."), *classes])
        if observed[classname, function + parameter + suffix] != 1:
            raise ValueError(f"Missing or repeated required case: {node}")
    return {"tasks": len(inventory), "required_cases": len(required), "report": totals}


def archived_report(phase: str) -> Path:
    gate = json.loads((ROOT / f"evidence/gates/{phase}.json").read_text())
    if (gate["task_id"] != f"gate.{phase}.completed" or gate["verification_status"] != "passed"
            or gate["executed"] is not True or not gate["source_sha256"]):
        raise ValueError("Invalid phase evidence")
    report = ROOT / gate["report_path"]
    files = {**gate["source_sha256"], gate["report_path"]: gate["report_sha256"]}
    for relative, expected in files.items():
        path = (ROOT / relative).resolve()
        if not path.is_relative_to(ROOT) or sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Snapshot differs from the checked-out file: {relative}")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase")
    reports = parser.add_mutually_exclusive_group()
    reports.add_argument("--report", type=Path, help="verify an existing passing JUnit report")
    reports.add_argument("--snapshot", action="store_true", help="check the recorded file hashes and their archived report")
    reports.add_argument("--junitxml", type=Path, help="retain the report from a new run")
    args = parser.parse_args()
    try:
        inventory = phase_cases(args.phase)
        with TemporaryDirectory() as tmp:
            if args.snapshot:
                report = archived_report(args.phase)
            elif args.report is None:
                report = (args.junitxml or Path(tmp) / "results.xml").resolve()
                # task -> exact cases -> one pytest invocation -> independently checked results.
                cases = sorted({node for nodes in inventory.values() for node in nodes})
                subprocess.run([sys.executable, "-m", "pytest", "-o", "addopts=", "-q",
                                f"--junitxml={report}", "--", *cases], cwd=ROOT, check=True)
            else:
                report = args.report
            print(json.dumps({"phase": args.phase, **verify_report(report, inventory)}, sort_keys=True))
    except (KeyError, OSError, TypeError, ValueError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
