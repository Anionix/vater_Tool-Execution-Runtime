"""TEXT-001: malformed Unicode input is always a safe, stable failure.

State: RAW -> TEXT_VALIDATED | REJECTED; rejected input never enters SQLite.
This tests local application behavior; it is NOT official MCP wire conformance.
"""
import sqlite3

import pytest

from verified_execution.core import LocalAuthority
from verified_execution.ledger import LedgerError, OperationLedger
from verified_execution.recorded_calculator import RecordedCalculator

BASE = dict(principal="alice", request_key="request-1", tool_name="checked_add",
            target_id=None, resource_version=None,
            arguments={"left": 1, "right": 2}, requires_approval=False)


@pytest.mark.parametrize("field", [
    "principal", "request_key", "tool_name", "target_id", "resource_version",
])
@pytest.mark.parametrize("invalid", ["\ud800", "\udfff", "foo\ud800bar"])
def test_unpaired_surrogate_rejected_without_database_side_effect(tmp_path, field, invalid):
    ledger = OperationLedger(tmp_path / "ledger.sqlite3")
    values = {**BASE, field: invalid}
    with pytest.raises(LedgerError) as error:
        ledger.reserve(**values)
    assert error.value.code == "INVALID_" + field.upper()
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


def test_approval_id_rejects_malformed_unicode_before_persist(tmp_path):
    ledger = OperationLedger(tmp_path / "ledger.sqlite3")
    with pytest.raises(LedgerError) as error:
        ledger.issue_approval(
            approval_id="\ud800", principal="alice", tool_name="checked_add",
            target_id=None, resource_version=None,
            arguments={"left": 1, "right": 2}, expires_at_ms=9999999999999,
        )
    assert error.value.code == "INVALID_APPROVAL_ID"
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0


def test_application_adapter_returns_stable_code_for_invalid_request_key(tmp_path):
    ledger = OperationLedger(tmp_path / "ledger.sqlite3")
    calc = RecordedCalculator(ledger, principal="local-user",
                              authority=LocalAuthority.calculator_only())
    result = calc.execute(request_key="\ud800", name="checked_add",
                          arguments={"left": 1, "right": 2})
    assert result.ok is False
    assert result.error_code == "INVALID_REQUEST_KEY"
    assert result.operation_id is None


def test_exact_utf8_byte_limit_for_multibyte_identifiers(tmp_path):
    ledger = OperationLedger(tmp_path / "ledger.sqlite3")
    accepted = ledger.reserve(**{**BASE, "request_key": "あ" * 85})
    assert accepted.state == "prepared"  # 255 bytes
    with pytest.raises(LedgerError) as error:
        ledger.reserve(**{**BASE, "request_key": "あ" * 86})
    assert error.value.code == "INVALID_REQUEST_KEY"  # 258 bytes
