"""Application-flow tests: host policy -> SQLite ledger -> real Python calculator."""
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import pytest

from verified_execution.core import LocalAuthority
from verified_execution.ledger import OperationLedger
from verified_execution.recorded_calculator import RecordedCalculator


@pytest.fixture
def fixture(tmp_path: Path):
    ledger = OperationLedger(tmp_path / "ledger.sqlite3")
    service = RecordedCalculator(
        ledger, principal="local-host-user",
        authority=LocalAuthority.calculator_only())
    return ledger, service


def test_success_is_durable(fixture):
    ledger, service = fixture
    result = service.execute(request_key="request-1", name="checked_add",
                             arguments={"left": 5, "right": 8})
    assert result.ok and result.value == 13
    assert result.ledger_state == "succeeded"
    assert ledger.get(result.operation_id).state == "succeeded"
    assert ledger.history(result.operation_id) == ["reserved", "dispatched", "definitive_succeeded"]


def test_overflow_is_definitive_failure(fixture):
    ledger, service = fixture
    result = service.execute(request_key="overflow-1", name="checked_add",
                             arguments={"left": 4294967295, "right": 1})
    assert result.error_code == "OVERFLOW"
    assert ledger.get(result.operation_id).state == "failed"


@pytest.mark.parametrize("name,args,code", [
    ("unknown", {"left": 1, "right": 2}, "UNKNOWN_TOOL"),
    ("checked_add", {"left": True, "right": 2}, "INVALID_ARGUMENT"),
    ("checked_add", {"left": 1.0, "right": 2}, "INVALID_ARGUMENT"),
    ("checked_add", {"left": 1, "right": 2, "extra": 0}, "INVALID_ARGUMENT"),
])
def test_invalid_requests_do_not_create_operations(fixture, name, args, code):
    ledger, service = fixture
    result = service.execute(request_key="test", name=name, arguments=args)
    assert not result.ok and result.error_code == code
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


def test_access_denied_does_not_create_operation(fixture):
    ledger, _ = fixture
    denied_service = RecordedCalculator(
        ledger, principal="local-host-user", authority=LocalAuthority(frozenset()))
    result = denied_service.execute(request_key="denied", name="checked_add",
                                    arguments={"left": 1, "right": 2})
    assert result.error_code == "NOT_AUTHORIZED"
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


def test_repeated_request_cannot_run_again(fixture):
    ledger, service = fixture
    first = service.execute(request_key="replay", name="checked_add",
                            arguments={"left": 2, "right": 3})
    second = service.execute(request_key="replay", name="checked_add",
                             arguments={"left": 2, "right": 3})
    assert first.ok
    assert second.ok and second.value == 5 and second.replayed
    assert first.operation_id == second.operation_id
    assert ledger.history(first.operation_id).count("dispatched") == 1


def test_request_collision_is_blocked(fixture):
    ledger, service = fixture
    first = service.execute(request_key="same", name="checked_add",
                            arguments={"left": 2, "right": 3})
    second = service.execute(request_key="same", name="checked_add",
                             arguments={"left": 9, "right": 3})
    assert first.ok
    assert second.error_code == "IDEMPOTENCY_CONFLICT"
    assert ledger.history(first.operation_id).count("dispatched") == 1


def test_separate_users_require_host_configuration(fixture):
    ledger, local = fixture
    second = RecordedCalculator(ledger, principal="other-host-user",
                                authority=LocalAuthority.calculator_only())
    one = local.execute(request_key="shared-key", name="checked_add",
                        arguments={"left": 1, "right": 1})
    two = second.execute(request_key="shared-key", name="checked_add",
                         arguments={"left": 1, "right": 1})
    assert one.ok and two.ok and one.operation_id != two.operation_id


def test_parallel_repeats_do_not_dispatch_twice(fixture):
    ledger, service = fixture
    def action(_):
        return service.execute(request_key="parallel", name="checked_add",
                               arguments={"left": 10, "right": 20})
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(action, range(20)))
    # A concurrent replay can arrive while the first execution is still
    # DISPATCHED. It may be IN_PROGRESS until the atomic completion commits.
    assert sum(not result.replayed for result in results) == 1
    assert sum(result.replayed for result in results) == 19
    assert all(result.ok or result.error_code == "IN_PROGRESS" for result in results)
    assert service.execute(request_key="parallel", name="checked_add",
                           arguments={"left": 10, "right": 20}).value == 30
    assert ledger.history(results[0].operation_id).count("dispatched") == 1
