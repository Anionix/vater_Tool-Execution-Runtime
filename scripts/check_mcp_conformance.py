#!/usr/bin/env python3
"""task.testing.022: check an official MCP conformance run (scripts/run_mcp_conformance.mjs).

  python scripts/check_mcp_conformance.py mcp-conformance

Fails when:
  * any frame of any scenario broke the upstream wire schema;
  * a scenario is missing from evidence/mcp-conformance/applicability.json;
  * a required scenario, or a required check, is not SUCCESS or INFO;
  * the run differs in any scenario or check status from the committed record
    evidence/mcp-conformance/results.json, or used another upstream commit.
The last rule keeps the committed per-version results equal to what the
suite really reports; a change in behaviour must update the record.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "evidence" / "mcp-conformance" / "results.json"
POLICY = ROOT / "evidence" / "mcp-conformance" / "applicability.json"
PASSING = {"SUCCESS", "INFO"}


def statuses(result: dict) -> dict[str, list[list[str]]]:
    return {scenario["name"]: [[check["id"], check["status"]] for check in scenario["checks"]]
            for scenario in result["scenarios"]}


def policy_problems(version: str, observed: dict[str, list[list[str]]], policy: dict) -> list[str]:
    problems = []
    for name, checks in observed.items():
        rule = policy["scenarios"].get(name)
        if rule is None:
            problems.append(f"{version} {name}: not listed in the applicability policy")
        elif rule.get("required"):
            if not checks or any(status not in PASSING for _check, status in checks):
                problems.append(f"{version} {name}: required scenario did not pass: {checks}")
        elif "required_checks" in rule:
            for wanted in rule["required_checks"]:
                found = [status for check, status in checks if check == wanted]
                if not found or any(status not in PASSING for status in found):
                    problems.append(f"{version} {name}: required check {wanted} did not pass: {found}")
        elif not rule.get("not_applicable") or not rule.get("reason"):
            problems.append(f"{version} {name}: a scenario that is not required needs a category and reason")
    return problems


def run_problems(run: dict[str, dict], record: dict, policy: dict) -> list[str]:
    problems = []
    if set(run) != set(record["versions"]):
        problems.append(f"versions {sorted(run)} differ from the record {sorted(record['versions'])}")
    for version, result in sorted(run.items()):
        if result["upstream"]["commit"] != record["upstream"]["commit"]:
            problems.append(f"{version}: upstream commit {result['upstream']['commit']} is not the recorded one")
        for scenario in result["scenarios"]:
            if scenario["wireErrors"]:
                problems.append(f"{version} {scenario['name']}: wire errors {scenario['wireErrors'][:3]}")
        observed = statuses(result)
        problems += policy_problems(version, observed, policy)
        expected = record["versions"].get(version, {})
        for name in sorted(set(observed) | set(expected)):
            if observed.get(name) != expected.get(name):
                problems.append(f"{version} {name}: observed {observed.get(name)} but recorded {expected.get(name)}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path, help="directory written by scripts/run_mcp_conformance.mjs")
    parser.add_argument("--record", type=Path, default=RECORD)
    parser.add_argument("--policy", type=Path, default=POLICY)
    args = parser.parse_args()
    run = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in sorted(args.results.glob("*.json"))}
    record = json.loads(args.record.read_text(encoding="utf-8"))
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    problems = run_problems(run, record, policy)
    for problem in problems:
        print(problem)
    required = sum(1 for rule in policy["scenarios"].values() if rule.get("required") or "required_checks" in rule)
    print(json.dumps({"versions": sorted(run), "required_scenarios": required, "problems": len(problems)}))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
