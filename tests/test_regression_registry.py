"""task.testing.026: known anomalies stay caught by continuous regression tests.

tests/regressions.json lists each known anomaly with its regression tests and
a mutant: an exact text replacement that re-introduces the anomaly. For every
entry this module copies the repository to a temporary directory, applies the
mutant there, and requires the entry's tests to FAIL. A control run first
requires all listed tests to PASS on an unmodified copy, so the failures can
only come from the mutants. The real working tree is never modified.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = json.loads((Path(__file__).parent / "regressions.json").read_text(encoding="utf-8"))
ENTRIES = REGISTRY["entries"]
IGNORE = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache", "node_modules", "*.egg-info")


def copy_repository(destination: Path) -> Path:
    shutil.copytree(ROOT, destination, ignore=IGNORE, symlinks=True)
    return destination


def run_tests(repository: Path, tests: list[str]) -> subprocess.CompletedProcess:
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONPATH"] = os.pathsep.join([str(repository / "src"), str(repository / "tests")])
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x", *tests],
                          cwd=repository, env=environment, capture_output=True, text=True, timeout=600)


def test_registry_entries_are_complete():
    ids = [entry["id"] for entry in ENTRIES]
    assert len(ids) == len(set(ids)) and len(ids) >= 10
    for entry in ENTRIES:
        assert entry["anomaly"] and entry["source"] and entry["found"] and entry["tests"]
        for node in entry["tests"]:
            file, _, rest = node.partition("::")
            tree = ast.parse((ROOT / file).read_text(encoding="utf-8"))
            names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            assert rest.split("[", 1)[0] in names, node
        mutant = entry["mutant"]
        assert mutant["find"] != mutant["replace"]
        assert (ROOT / mutant["file"]).read_text(encoding="utf-8").count(mutant["find"]) == 1, entry["id"]


def test_regression_tests_pass_without_any_mutant(tmp_path):
    repository = copy_repository(tmp_path / "repository")
    tests = sorted({node for entry in ENTRIES for node in entry["tests"]})
    completed = run_tests(repository, tests)
    assert completed.returncode == 0, completed.stdout[-3000:]


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda entry: entry["id"])
def test_the_anomaly_is_detected_when_it_returns(entry, tmp_path):
    repository = copy_repository(tmp_path / "repository")
    target = repository / entry["mutant"]["file"]
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace(entry["mutant"]["find"], entry["mutant"]["replace"], 1), encoding="utf-8")
    completed = run_tests(repository, entry["tests"])
    # pytest exit status 1 means tests ran and failed (not a collection or usage error).
    assert completed.returncode == 1, (entry["id"], completed.stdout[-3000:])
    assert "= FAILURES =" in completed.stdout and "= ERRORS =" not in completed.stdout
