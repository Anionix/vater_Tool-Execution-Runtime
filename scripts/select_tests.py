#!/usr/bin/env python3
"""task.operations.002: decide which tests a change must run.

  python scripts/select_tests.py PATH ...            # the changed paths
  python scripts/select_tests.py --base origin/main  # the paths changed since BASE
  python scripts/select_tests.py --base origin/main --run

Rules (config/test_selection.json): a permission change (risk classes, tool
contracts, launcher approvals, limits, the runtime and adapters, workflow
token permissions) always adds the safety suite. Source, contract and
configuration changes run the whole suite, as does any path no rule explains.
A test file runs itself; a script, document or evidence file runs the tests
that name it, and the whole suite if none does. The selection is never empty.
"""
from __future__ import annotations

import argparse
from fnmatch import fnmatch
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "config" / "test_selection.json"
FULL = "tests"
NAMED_BY_TESTS = ("scripts/", "docs/", "evidence/", ".github/")


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch(path, pattern) or (pattern.endswith("/**") and path.startswith(pattern[:-2]))
               for pattern in patterns)


def tests_naming(path: str, root: Path = ROOT) -> list[str]:
    """Test files whose text names the path (or, for a script or workflow, its file name)."""
    needles = {path}
    if path.startswith(("scripts/", ".github/")):
        needles.add(Path(path).name)
    return sorted(str(test.relative_to(root)) for test in (root / "tests").glob("test_*.py")
                  if any(needle in test.read_text(encoding="utf-8") for needle in needles))


def select(changed: list[str], rules: dict | None = None, root: Path = ROOT) -> dict:
    rules = rules or json.loads(RULES.read_text(encoding="utf-8"))
    tests: set[str] = set()
    reasons: dict[str, str] = {}
    permission_change, full = False, not changed
    for path in sorted(set(changed)):
        why = []
        permission = matches(path, rules["permission_paths"])
        if permission:
            permission_change = True
            tests.update(rules["safety_suite"])
            why.append("permission change: safety suite")
        if matches(path, rules["full_suite_paths"]):
            full = True
            why.append("whole suite")
        elif fnmatch(path, "tests/test_*.py"):
            tests.add(path)
            why.append("test file")
        else:
            naming = tests_naming(path, root) if path.startswith(NAMED_BY_TESTS) else []
            if naming:
                tests.update(naming)
                why.append("tests that name it")
            elif not permission:
                full = True
                why.append("no rule explains it: whole suite")
        reasons[path] = "; ".join(why)
    return {"changed": sorted(set(changed)), "permission_change": permission_change,
            "safety_suite": sorted(rules["safety_suite"]) if permission_change else [],
            "full": full, "tests": [FULL] if full else sorted(tests), "reasons": reasons}


def changed_since(base: str, root: Path = ROOT) -> list[str]:
    output = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=root, check=True,
                            capture_output=True, text=True).stdout
    return [line for line in output.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--base")
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    changed = args.paths + (changed_since(args.base) if args.base else [])
    selection = select(changed)
    print(json.dumps(selection, indent=1))
    if not args.run:
        return 0
    # The safety suite is named explicitly even inside a whole-suite run, so narrowing
    # the whole suite later can never drop it.
    targets = sorted(set(selection["tests"]) | set(selection["safety_suite"]))
    return subprocess.run([sys.executable, "-m", "pytest", "-q", *targets], cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
