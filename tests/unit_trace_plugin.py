"""pytest plugin for task.testing.001: which public function each test exercises, and how.

Load explicitly:  VATER_UNIT_TRACE=out.json python -m pytest -p unit_trace_plugin ...

Uses sys.monitoring (Python 3.12+). For every public function of the
implementation (see public_functions()), it records per test whether the
function returned normally ("return"), returned an error value
("error_return"), or raised ("raise"). Calls of any other code are disabled
at their first event, so the cost outside the target functions is one event
per code location.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("src/verified_execution/*.py", "server_mcp.py")
TOOL = 3  # sys.monitoring tool ids 3 and 4 are not reserved


def public_functions() -> dict[tuple[str, str], str]:
    """(absolute file, qualname) -> '<relative file>::<qualname>' for module functions and
    class methods whose names do not start with '_' (Protocol stubs excluded)."""
    found = {}
    for pattern in SOURCES:
        for path in sorted(ROOT.glob(pattern)):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                    found[str(path), node.name] = f"{path.relative_to(ROOT)}::{node.name}"
                if isinstance(node, ast.ClassDef) and not node.name.startswith("_") and not any(
                        isinstance(base, ast.Name) and base.id == "Protocol" for base in node.bases):
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and not item.name.startswith("_"):
                            qualname = f"{node.name}.{item.name}"
                            found[str(path), qualname] = f"{path.relative_to(ROOT)}::{qualname}"
    return found


def error_like(value) -> bool:
    """A returned value that reports a refusal or failure rather than a result."""
    if value is False:
        return True
    for name in ("error_code", "error"):
        attribute = getattr(value, name, None)
        if isinstance(attribute, str) and attribute:
            return True
    if getattr(value, "ok", True) is False or getattr(value, "succeeded", True) is False:
        return True
    if getattr(value, "kind", None) in ("rejected", "unknown", "not_executed"):
        return True
    for name in ("status", "status_code"):
        status = getattr(value, name, None)
        if type(status) is int and status >= 400:
            return True
    if isinstance(value, dict) and (value.get("error") or value.get("error_code")):
        return True
    return False


class Recorder:
    def __init__(self, output: Path):
        self.output, self.targets, self.current, self.seen = output, public_functions(), None, {}
        self.disable = sys.monitoring.DISABLE

    def _mark(self, code, event):
        name = self.targets.get((code.co_filename, code.co_qualname))
        if name is None:
            return False
        if self.current is not None:
            self.seen.setdefault(self.current, {}).setdefault(name, set()).add(event)
        return True

    def on_start(self, code, offset):
        return None if self._mark(code, "call") else self.disable

    def on_return(self, code, offset, value):
        if (code.co_filename, code.co_qualname) not in self.targets:
            return self.disable
        self._mark(code, "error_return" if error_like(value) else "return")
        if value is None or (isinstance(value, tuple) and value and all(item is None for item in value)):
            self._mark(code, "none")  # an absent result, e.g. a lookup that found nothing
        return None

    def on_unwind(self, code, offset, exception):
        self._mark(code, "raise")

    def start(self):
        events = sys.monitoring.events
        sys.monitoring.use_tool_id(TOOL, "vater-unit-trace")
        sys.monitoring.register_callback(TOOL, events.PY_START, self.on_start)
        sys.monitoring.register_callback(TOOL, events.PY_RETURN, self.on_return)
        sys.monitoring.register_callback(TOOL, events.PY_UNWIND, self.on_unwind)
        sys.monitoring.set_events(TOOL, events.PY_START | events.PY_RETURN | events.PY_UNWIND)

    def stop(self):
        sys.monitoring.set_events(TOOL, 0)
        sys.monitoring.free_tool_id(TOOL)
        data = {test: {name: sorted(events) for name, events in functions.items()}
                for test, functions in self.seen.items()}
        self.output.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")


RECORDER: Recorder | None = None


def pytest_configure(config):
    global RECORDER
    output = os.environ.get("VATER_UNIT_TRACE")
    if output:
        RECORDER = Recorder(Path(output))
        RECORDER.start()


def pytest_unconfigure(config):
    if RECORDER is not None:
        RECORDER.stop()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    if RECORDER is not None:
        RECORDER.current = item.nodeid
    try:
        return (yield)
    finally:
        if RECORDER is not None:
            RECORDER.current = None
