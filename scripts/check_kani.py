#!/usr/bin/env python3
"""task.formal_verification.011 / 012 / 013: require every audited Kani harness to verify with all covers satisfied.

  python scripts/check_kani.py kani-output.txt [--record evidence/formal/kani-results.json]

Reads the output of `cargo kani` and, for every harness of docs/kani_assumptions.json,
requires VERIFICATION:- SUCCESSFUL and every one of its cover statements satisfied (an
unsatisfied or unreachable cover means an assumption or the code removed that case). No
other harness may appear, and Kani must report no failure. With --record, the parsed
results must also equal the committed record exactly.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "docs" / "kani_assumptions.json"


def parse(text: str) -> dict:
    results: dict[str, dict] = {}
    current = None
    for line in text.splitlines():
        if match := re.search(r"Checking harness (\S+?)\.\.\.", line):
            current = match.group(1).split("::")[-1]
            results[current] = {"verification": None, "covers_satisfied": None, "covers_total": None}
        elif current and (match := re.search(r"\*\* (\d+) of (\d+) cover propert(?:y|ies) satisfied", line)):
            results[current]["covers_satisfied"], results[current]["covers_total"] = map(int, match.groups())
        elif current and (match := re.search(r"VERIFICATION:- (SUCCESSFUL|FAILED)", line)):
            results[current]["verification"] = match.group(1)
    summary = re.search(r"Complete - (\d+) successfully verified harnesses, (\d+) failures, (\d+) total", text)
    return {"harnesses": results,
            "summary": dict(zip(("verified", "failures", "total"), map(int, summary.groups()))) if summary else None}


def problems(parsed: dict, audit: dict) -> list[str]:
    found = []
    expected = audit["harnesses"]
    if set(parsed["harnesses"]) != set(expected):
        found.append(f"harnesses differ from the audit: ran {sorted(parsed['harnesses'])}")
    for name, entry in expected.items():
        result = parsed["harnesses"].get(name)
        if result is None:
            continue
        if result["verification"] != "SUCCESSFUL":
            found.append(f"{name}: {result['verification']}")
        if result["covers_total"] != len(entry["covers"]) or result["covers_satisfied"] != len(entry["covers"]):
            found.append(f"{name}: covers {result['covers_satisfied']} of {result['covers_total']} satisfied, "
                         f"{len(entry['covers'])} audited")
    if parsed["summary"] is None or parsed["summary"]["failures"] != 0 or parsed["summary"]["total"] != len(expected):
        found.append(f"summary: {parsed['summary']}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--record", type=Path)
    args = parser.parse_args(argv)
    parsed = parse(args.output.read_text(encoding="utf-8", errors="replace"))
    found = problems(parsed, json.loads(AUDIT.read_text(encoding="utf-8")))
    if args.record is not None:
        recorded = json.loads(args.record.read_text(encoding="utf-8"))
        if {key: recorded[key] for key in ("harnesses", "summary")} != parsed:
            found.append("the run differs from the committed record")
    print(json.dumps({**parsed, "problems": found}, indent=1))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
