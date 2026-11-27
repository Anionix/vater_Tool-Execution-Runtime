"""task.testing.001: every public function has a normal and an abnormal test.

tests/unit_inventory.json maps each public function (module functions and
class methods without a leading underscore, in src/verified_execution and
server_mcp.py) to the tests for its normal case and its abnormal case. The
list of public functions is computed from the source here, so a new function
without an entry fails. Then the listed tests are run in a subprocess under
tests/unit_trace_plugin.py, and each function must show the declared
expectation in at least one of its listed tests:

  return       returned normally
  raise        raised an exception out of the function
  error_value  returned a refusal (an error code, ok/succeeded False, or False)
  absent       returned None (or only Nones) for something that does not exist
  handled      returned normally for an abnormal input or state (total
               functions; the entry's note explains why there is no failure)
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from unit_trace_plugin import error_like, public_functions

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = json.loads((Path(__file__).parent / "unit_inventory.json").read_text(encoding="utf-8"))["functions"]
EVENT = {"return": "return", "handled": "return", "raise": "raise", "error_value": "error_return", "absent": "none"}


def listed_tests() -> list[str]:
    return sorted({test for entry in INVENTORY.values() for side in entry.values() for test in side["tests"]})


def test_every_public_function_is_listed():
    assert sorted(INVENTORY) == sorted(public_functions().values())


def test_every_entry_has_both_cases():
    for function, entry in INVENTORY.items():
        assert set(entry) == {"normal", "abnormal"}, function
        for side in entry.values():
            assert side["expect"] in EVENT and side["tests"], function
            if side["expect"] == "handled":
                assert side.get("note"), function
        assert entry["normal"]["expect"] in {"return", "raise"}, function
        if entry["normal"]["expect"] == "raise":
            assert entry["normal"].get("note"), function  # only a documented refusal


def test_listed_tests_exist():
    for node in listed_tests():
        file, _, rest = node.partition("::")
        name = rest.split("[", 1)[0]
        tree = ast.parse((ROOT / file).read_text(encoding="utf-8"))
        assert name in {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}, node


def test_listed_tests_show_each_expectation(tmp_path):
    """Run exactly the listed tests under the tracer; every expectation must be observed."""
    output = tmp_path / "trace.json"
    environment = dict(os.environ, VATER_UNIT_TRACE=str(output),
                       PYTHONPATH=os.pathsep.join(filter(None, [str(ROOT / "tests"), os.environ.get("PYTHONPATH")])))
    completed = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                "-p", "unit_trace_plugin", *listed_tests()],
                               cwd=ROOT, env=environment, capture_output=True, text=True, timeout=900)
    assert completed.returncode == 0, completed.stdout[-4000:]
    trace = json.loads(output.read_text(encoding="utf-8"))
    missing = []
    for function, entry in INVENTORY.items():
        for side, declared in entry.items():
            wanted = EVENT[declared["expect"]]
            if not any(wanted in trace.get(test, {}).get(function, []) for test in declared["tests"]):
                missing.append(f"{function} {side}: no listed test shows {declared['expect']}")
    assert missing == []


@pytest.mark.parametrize("value,expected", [
    (False, True), (None, False), (0, False), ({"error": "X"}, True), ({"result": 1}, False)])
def test_error_value_classification(value, expected):
    assert error_like(value) is expected
