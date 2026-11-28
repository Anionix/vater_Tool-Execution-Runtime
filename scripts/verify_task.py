#!/usr/bin/env python3
"""task.operations.003: one change is one independent check unit.

  python scripts/verify_task.py task.http_interface.003 [--junitxml report.xml]

A change is reviewed and checked as one task: its single evidence record
(validated exactly as scripts/verify_phase.py validates it) names its
acceptance criterion and its exact pytest cases. This script runs those
cases, and nothing else, in a new pytest process without a cache, requires
every case to pass exactly once, and reports the change's review scope: the
files bound to the record and whether each still has its recorded SHA-256.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from verify_phase import ROOT, load_tasks, task_cases, verify_report


def review_scope(task_id: str, tasks: dict) -> dict[str, str]:
    scope = {}
    for relative in tasks[task_id]["evidence_paths"]:
        record = json.loads((ROOT / relative).read_text(encoding="utf-8"))
        for name, digest in (record.get("source_sha256") or {}).items():
            path = ROOT / name
            scope[name] = ("missing" if not path.is_file()
                           else "unchanged" if sha256(path.read_bytes()).hexdigest() == digest else "changed")
    return scope


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    parser.add_argument("--junitxml", type=Path)
    args = parser.parse_args(argv)
    try:
        tasks = load_tasks()
        if args.task_id not in tasks or not args.task_id.startswith("task."):
            raise ValueError(f"Unknown task: {args.task_id}")
        inventory = {args.task_id: task_cases(tasks, args.task_id)}
        with TemporaryDirectory() as tmp:
            report = (args.junitxml or Path(tmp) / "results.xml").resolve()
            subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", "-q",
                            f"--junitxml={report}", "--", *inventory[args.task_id]], cwd=ROOT, check=True)
            result = verify_report(report, inventory)
    except (KeyError, OSError, TypeError, ValueError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps({"task_id": args.task_id, **result, "review_scope": review_scope(args.task_id, tasks)},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
