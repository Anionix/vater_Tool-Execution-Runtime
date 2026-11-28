#!/usr/bin/env python3
"""task.operations.014: run every required check, for example against a new dependency set.

  .venv/bin/python scripts/required_checks.py [--workflow FILE] [--list]

The required checks are, by definition, the run steps of the release
verification workflow (.github/workflows/python-verification.yml), except its
dependency installs. This script reads them from that file, so the list
cannot drift, and runs each one with the interpreter running this script:
inside a virtual environment that holds a candidate lock, every check runs on
the new versions. All checks run; the exit status is 1 if any failed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "python-verification.yml"
# Jobs that do not use the runtime dependency set: release-artifacts builds with its own
# hash-locked backend (requirements-build.lock) and formal-rust checks the Rust port with its
# own toolchain, so a runtime candidate changes neither.
INDEPENDENT_OF_RUNTIME_DEPENDENCIES = frozenset({"release-artifacts", "formal-rust"})


def run_steps(text: str) -> list[dict]:
    """(job, step name, command) of every run step; folded '>-' blocks are joined."""
    steps, job, name = [], None, None
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if match := re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line):
            job = match.group(1)
        elif match := re.match(r"^\s*- name:\s*(.+?)\s*$", line):
            name = match.group(1)
        elif match := re.match(r"^(\s*)(?:- )?run:\s*(.*?)\s*$", line):
            indent, value = len(match.group(1)), match.group(2)
            if value in (">-", ">", "|", "|-"):
                block = []
                while index + 1 < len(lines) and (not lines[index + 1].strip()
                                                  or len(lines[index + 1]) - len(lines[index + 1].lstrip()) > indent):
                    index += 1
                    if lines[index].strip():
                        block.append(lines[index].strip())
                value = (" " if value.startswith(">") else "\n").join(block)
            steps.append({"job": job, "name": name, "command": value})
        index += 1
    return steps


def required_commands(workflow: Path = WORKFLOW) -> list[dict]:
    return [step for step in run_steps(workflow.read_text(encoding="utf-8"))
            if not re.search(r"\bpip install\b", step["command"])
            and step["job"] not in INDEPENDENT_OF_RUNTIME_DEPENDENCIES]


def with_interpreter(command: str, python: str = sys.executable) -> str:
    return re.sub(r"(^|\n|&& |; )python(?= )", lambda m: m.group(1) + shlex.quote(python), command)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", type=Path, default=WORKFLOW)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    steps = required_commands(args.workflow)
    if args.list:
        print(json.dumps(steps, indent=1))
        return 0
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(ROOT / "src"),
                                                                           os.environ.get("PYTHONPATH")])))
    failed = []
    for step in steps:
        command = with_interpreter(step["command"])
        print(f"::group::{step['job']}: {step['name']}", flush=True)
        completed = subprocess.run(["bash", "-c", command], cwd=ROOT, env=environment)
        print("::endgroup::", flush=True)
        if completed.returncode != 0:
            failed.append({**step, "returncode": completed.returncode})
    print(json.dumps({"interpreter": sys.executable, "commands": len(steps), "failed": failed}, indent=1))
    return 1 if failed or not steps else 0


if __name__ == "__main__":
    sys.exit(main())
