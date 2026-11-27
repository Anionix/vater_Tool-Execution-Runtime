#!/usr/bin/env python3
"""task.operations.017: decide whether the extended release, with the added features, may be published.

  python scripts/release_decision.py --output release-decision.json [--github-output FILE] [--require extended]

The required gates come from the task ledger itself:
  initial release   every phase of the initial Python version (implementation_stage python_minimum)
  extended release  the initial gates plus every required added-feature phase (post_minimum,
                    except the optional_* phases)
A gate passes only if the ledger marks it done and each of its evidence
records says executed and passed and is still bound to the current files
(scripts/evidence_freshness.py): stale evidence does not count. The extended
release is allowed only when every initial and every added-feature gate
passes. The release job publishes the extended artifacts only then.
"""
from __future__ import annotations

import argparse
from importlib import util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = util.spec_from_file_location("evidence_freshness", ROOT / "scripts" / "evidence_freshness.py")
evidence_freshness = util.module_from_spec(spec)
spec.loader.exec_module(evidence_freshness)


def load_tasks(root: Path = ROOT) -> dict[str, dict]:
    rows = [json.loads(line) for line in (root / "backlog" / "tasks.jsonl").read_text(encoding="utf-8").splitlines()]
    return {row["task_id"]: row for row in rows}


def required_gates(tasks: dict[str, dict]) -> dict[str, list[str]]:
    stages: dict[str, set[str]] = {}
    for row in tasks.values():
        stages.setdefault(row["phase"], set()).add(row["implementation_stage"])
    initial = sorted(f"gate.{phase}.completed" for phase, stage in stages.items() if stage == {"python_minimum"})
    added = sorted(f"gate.{phase}.completed" for phase, stage in stages.items()
                   if stage == {"post_minimum"} and not phase.startswith("optional_"))
    return {"initial": [gate for gate in initial if gate in tasks], "added": [gate for gate in added if gate in tasks]}


def evaluate(gate_id: str, tasks: dict[str, dict], root: Path = ROOT) -> dict:
    row, reasons, records = tasks[gate_id], [], []
    if row["status"] != "done":
        reasons.append(f"gate is {row['status']}")
    if not row["evidence_paths"]:
        reasons.append("no gate evidence")
    for relative in row["evidence_paths"]:
        path = root / relative
        if not path.is_file():
            reasons.append(f"missing evidence {relative}")
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        state = evidence_freshness.freshness(path, root)
        records.append({"path": relative, "freshness": state["state"]})
        if record.get("executed") is not True or record.get("verification_status") != "passed":
            reasons.append(f"{relative} is not an executed, passed record")
        if state["state"] != "fresh":
            reasons.append(f"{relative} is {state['state']}" + (f": {', '.join(state['changed'][:5])}"
                                                                 if state["changed"] else ""))
    return {"status": row["status"], "records": records, "passed": not reasons, "reasons": reasons}


def decide(root: Path = ROOT, tasks: dict[str, dict] | None = None) -> dict:
    tasks = tasks or load_tasks(root)
    gates = required_gates(tasks)
    initial = {gate: evaluate(gate, tasks, root) for gate in gates["initial"]}
    added = {gate: evaluate(gate, tasks, root) for gate in gates["added"]}
    initial_allowed = bool(initial) and all(result["passed"] for result in initial.values())
    extended_allowed = initial_allowed and bool(added) and all(result["passed"] for result in added.values())
    return {"initial_release": {"allowed": initial_allowed, "gates": initial},
            "extended_release": {"allowed": extended_allowed, "added_gates": added,
                                 "blocked_by": sorted(gate for gate, result in {**initial, **added}.items()
                                                      if not result["passed"])}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--require", choices=["initial", "extended"])
    args = parser.parse_args(argv)
    decision = decide()
    args.output.write_text(json.dumps(decision, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    initial, extended = decision["initial_release"]["allowed"], decision["extended_release"]["allowed"]
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(f"initial={str(initial).lower()}\nextended={str(extended).lower()}\n")
    print(json.dumps({"initial_release": initial, "extended_release": extended,
                      "blocked_by": decision["extended_release"]["blocked_by"]}, indent=1))
    if args.require == "extended" and not extended or args.require == "initial" and not initial:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
