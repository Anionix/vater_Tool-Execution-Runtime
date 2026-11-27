"""task.testing.019: the concurrency inventory covers every racing site.

docs/concurrency_inventory.json lists the thread, process, SQLite and async
races of the Python implementation. This module scans the source on its own
for concurrency primitives, so a new lock, thread, subprocess, task or SQLite
setting that is not listed fails here. Each 'tested' entry must name tests
that exist; each 'unchecked' entry must name none and say why, so an
unchecked order can never be presented as guaranteed.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = json.loads((ROOT / "docs" / "concurrency_inventory.json").read_text(encoding="utf-8"))
ENTRIES = INVENTORY["entries"]
PRIMITIVES = re.compile(r"\b(Lock|RLock|Event|Thread|Condition|Semaphore|Barrier|CapacityLimiter|to_thread|"
                        r"run_in_executor|create_task|TaskGroup|start_soon|Popen|multiprocessing|BEGIN IMMEDIATE|"
                        r"isolation_level|busy_timeout|flock|fsync|select\.select)\b")
SOURCES = ["src/verified_execution/*.py", "server_mcp.py", "scripts/guarded_stdio.py",
           "scripts/run_local_mcp.py", "scripts/launch_guarded_mcp.py"]


def scopes(tree):
    found = []

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, prefix + [child.name])
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append((child.lineno, child.end_lineno, ".".join(prefix + [child.name])))
                visit(child, prefix + [child.name])
            else:
                visit(child, prefix)
    visit(tree, [])
    return found


def concurrency_sites() -> dict[str, set[str]]:
    """'<file>::<innermost qualified function>' for every line that uses a primitive."""
    found: dict[str, set[str]] = {}
    for pattern in SOURCES:
        for path in sorted(ROOT.glob(pattern)):
            text = path.read_text(encoding="utf-8")
            spans = scopes(ast.parse(text))
            for number, line in enumerate(text.splitlines(), 1):
                for match in PRIMITIVES.finditer(line.split("#", 1)[0]):
                    inner = [name for start, end, name in spans if start <= number <= end]
                    site = f"{path.relative_to(ROOT)}::{max(inner, key=len) if inner else '<module>'}"
                    found.setdefault(site, set()).add(match.group(1))
    return found


def defined_tests(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")}


def test_scanner_finds_the_known_primitives():
    sites = concurrency_sites()
    assert "BEGIN IMMEDIATE" in sites["src/verified_execution/ledger.py::OperationLedger._transaction"]
    assert "Popen" in sites["scripts/guarded_stdio.py::main"]
    assert len(sites) >= 20


def test_every_racing_site_is_in_the_inventory():
    listed = {site for entry in ENTRIES for site in entry["sites"]}
    found = concurrency_sites()
    assert sorted(set(found) - listed) == []
    # Listed sites must still exist, so the inventory cannot silently go stale either.
    assert sorted(listed - set(found)) == []


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda entry: entry["id"])
def test_entry_is_complete_and_honest(entry):
    assert entry["kind"] in {"sqlite", "thread", "process", "async"}
    assert entry["race"] and entry["guard"]
    assert len({e["id"] for e in ENTRIES}) == len(ENTRIES)
    if entry["status"] == "tested":
        assert entry["sites"] and entry["tests"]
        for node in entry["tests"]:
            file, _, name = node.partition("::")
            assert (ROOT / file).is_file() and name in defined_tests(ROOT / file), node
    else:
        assert entry["status"] == "unchecked"
        # An unchecked order names no test and explains itself: it is not claimed as guaranteed.
        assert entry["tests"] == [] and entry["reason"]


def test_the_kinds_the_task_names_are_all_covered():
    kinds = {(entry["kind"], entry["status"]) for entry in ENTRIES}
    for kind in ("sqlite", "thread", "process", "async"):
        assert (kind, "tested") in kinds
    assert any(entry["status"] == "unchecked" for entry in ENTRIES)
