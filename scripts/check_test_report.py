#!/usr/bin/env python3
"""Require a real passing pytest run, not a skipped protocol suite.

TEST-GATE-001: ABSENT -> REJECTED; READ -> VALIDATED -> PASSED | REJECTED.
TEST-GATE-002: no failures, errors, skipped, malformed or missing testcase records.
TEST-GATE-003: count actual testcase entries; do not trust only suite summaries.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from xml.etree import ElementTree


def check_report(path: Path, *, minimum: int) -> dict[str, int]:
    if type(minimum) is not int or minimum < 1:
        raise ValueError("INVALID_MINIMUM")
    try:
        with path.open("rb") as reader:
            data = reader.read(8_000_001)
    except OSError as exc:
        raise ValueError("REPORT_UNAVAILABLE") from exc
    if len(data) > 8_000_000:
        raise ValueError("REPORT_TOO_LARGE")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise ValueError("INVALID_XML") from exc
    if root.tag not in {"testsuite", "testsuites"}:
        raise ValueError("INVALID_REPORT_ROOT")
    suites = list(root.iter("testsuite"))
    if not suites:
        raise ValueError("MISSING_TEST_SUITE")
    cases = list(root.iter("testcase"))
    if len(cases) < minimum:
        raise ValueError("INSUFFICIENT_TESTS")
    if any(not case.get("name") or not case.get("classname") for case in cases):
        raise ValueError("INVALID_TESTCASE")
    failures = sum(len(case.findall("failure")) for case in cases)
    errors = sum(len(case.findall("error")) for case in cases)
    skipped = sum(len(case.findall("skipped")) for case in cases)
    if failures or errors or skipped:
        raise ValueError("NOT_ALL_TESTS_PASSED")
    for suite in suites:
        # Fail closed if pytest's recorded totals disagree with emitted cases.
        children = list(suite.iter("testcase"))
        for attribute, actual in (("tests", len(children)), ("failures", 0), ("errors", 0), ("skipped", 0)):
            try:
                reported = int(suite.attrib[attribute])
            except (KeyError, ValueError) as exc:
                raise ValueError("INVALID_SUITE_TOTALS") from exc
            if reported != actual:
                raise ValueError("INCONSISTENT_SUITE_TOTALS")
    return {"passed": len(cases), "failed": 0, "errors": 0, "skipped": 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail on skipped or missing integration tests")
    parser.add_argument("report", type=Path)
    parser.add_argument("--minimum", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        result = check_report(args.report, minimum=args.minimum)
    except ValueError as exc:
        print(json.dumps({"state": "rejected", "rule": str(exc)}))
        return 1
    print(json.dumps({"state": "passed", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
