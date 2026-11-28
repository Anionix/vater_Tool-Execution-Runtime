"""task.formal_verification.015: Python and the Rust port are compared on the same literal expected values.

tests/fixtures/independent_expected_values.txt holds expected outcomes written as literals
from the documented policy, never computed from an implementation. This suite checks the
Python implementation (core.checked_add, core.ALLOWED_TRANSITIONS, LocalAuthority.permits
and the real ledger's approval check) against every row; the Rust port's own suite
(rust/vater_core/tests/expected_values.rs, run by the formal-rust CI job) reads the same file.
Both passing means both implementations agree on exactly these inputs.
"""
from __future__ import annotations

from pathlib import Path
import re
from threading import Event

import pytest

from verified_execution import core
from verified_execution.core import ALLOWED_TRANSITIONS, LOCAL_PUBLISHER, LocalAuthority, OperationError, State
from verified_execution.ledger import LedgerError, OperationLedger

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "independent_expected_values.txt"
RUST_SUITE = ROOT / "rust" / "vater_core" / "tests" / "expected_values.rs"
ROWS = [line.split() for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")]
NOW_NS = 1_000_000
NOW_MS = 1_000


def row_id(row):
    return "-".join(row)


def python_add(left: str, right: str) -> str:
    try:
        return f"ok:{core.checked_add(int(left), int(right))}"
    except OperationError as error:
        return error.code


def python_permit(monkeypatch, publisher, tool, destination, revoked, deadline, tools, destinations) -> str:
    monkeypatch.setattr(core, "monotonic_ns", lambda: NOW_NS)
    event = Event()
    if revoked == "yes":
        event.set()
    expires = {"-": None, "future": NOW_NS + 1, "now": NOW_NS, "past": NOW_NS - 1}[deadline]
    grant = LocalAuthority(frozenset() if tools == "-" else frozenset(tools.split(",")),
                           allowed_destinations=frozenset() if destinations == "-" else frozenset(destinations.split(",")),
                           expires_at_monotonic_ns=expires, revocation_event=event)
    allowed = grant.permits(tool, publisher=LOCAL_PUBLISHER if publisher == "local" else "someone/else",
                            destination=None if destination == "-" else destination)
    return "allow" if allowed else "deny"


def python_approval(tmp_path, monkeypatch, differs, used, expired) -> str:
    clock = {"now": NOW_MS}
    monkeypatch.setattr(OperationLedger, "_now", staticmethod(lambda: clock["now"]))
    ledger = OperationLedger(tmp_path / "ledger.sqlite3")
    binding = {"principal": "p", "tool_name": "t", "target_id": "x", "resource_version": "v"}

    def reserve(key):
        return ledger.reserve(**binding, request_key=key, arguments={"n": 1}, requires_approval=True)

    operation = reserve("k-1")
    approved = dict(binding, arguments={"n": 1})
    changed = {"principal": ("principal", "q"), "tool": ("tool_name", "u"), "target": ("target_id", "y"),
               "version": ("resource_version", "w"), "arguments": ("arguments", {"n": 2})}
    if differs in changed:
        key, value = changed[differs]
        approved[key] = value
    if differs != "absent":
        ledger.issue_approval(approval_id="a-1", expires_at_ms=NOW_MS + 1, **approved)
    if used == "yes":
        ledger.begin_dispatch(reserve("k-0").operation_id, approval_id="a-1")  # a twin uses it first
    if expired == "yes":
        clock["now"] = NOW_MS + 1  # the ledger clock reaches the expiry
    try:
        ledger.begin_dispatch(operation.operation_id, approval_id="a-1")
    except LedgerError as error:
        assert error.code == "APPROVAL_INVALID"
        return "refuse"
    return "admit"


@pytest.mark.parametrize("row", ROWS, ids=row_id)
def test_python_produces_every_literal_expected_value(row, tmp_path, monkeypatch):
    kind, *inputs, expected = row
    if kind == "add":
        got = python_add(*inputs)
    elif kind == "transition":
        got = "allowed" if (State(inputs[0]), State(inputs[1])) in ALLOWED_TRANSITIONS else "forbidden"
    elif kind == "permit":
        got = python_permit(monkeypatch, *inputs)
    elif kind == "approval":
        got = python_approval(tmp_path, monkeypatch, *inputs)
    else:
        raise AssertionError(f"unknown row {row}")
    assert got == expected


def test_the_fixture_covers_every_transition_and_each_kind_of_decision():
    transitions = [(row[1], row[2]) for row in ROWS if row[0] == "transition"]
    names = [state.value for state in State]
    assert sorted(transitions) == sorted((a, b) for a in names for b in names)
    assert sum(1 for row in ROWS if row[0] == "transition" and row[3] == "allowed") == 7
    kinds = {row[0] for row in ROWS}
    assert kinds == {"add", "transition", "permit", "approval"} and len(ROWS) >= 80
    for kind in kinds:
        outcomes = {row[-1] for row in ROWS if row[0] == kind}
        assert len(outcomes) >= 2, (kind, outcomes)  # each kind has a success and a refusal


def test_expected_values_are_literals_not_formulas():
    for row in ROWS:
        for field in row:
            assert re.fullmatch(r"[A-Za-z0-9_:,\-]+", field), row  # no operators or expressions


def test_the_rust_port_reads_the_same_fixture_and_handles_every_kind():
    rust = RUST_SUITE.read_text(encoding="utf-8")
    assert '"../../tests/fixtures/independent_expected_values.txt"' in rust
    for kind in ("add", "transition", "permit", "approval"):
        assert f'["{kind}",' in rust, kind
