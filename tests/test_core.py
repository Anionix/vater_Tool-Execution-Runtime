import itertools
import pytest
from verified_execution.core import (
    ALLOWED_TRANSITIONS, State, ExecutionTrace, LocalAuthority,
    CalculationService, OperationError, checked_add, require_unsigned_32,
    validated_arguments, UNSIGNED_32_MAX,
)


def test_all_states_have_exact_edge_rules():
    for frm, to in itertools.product(State, repeat=2):
        trace = ExecutionTrace()
        trace.states = [frm]
        if (frm, to) in ALLOWED_TRANSITIONS:
            trace.move(to)
            assert trace.states[-1] == to
        else:
            with pytest.raises(OperationError) as error:
                trace.move(to)
            assert error.value.code == "INVALID_TRANSITION"


@pytest.mark.parametrize("left,right,result", [(0, 0, 0), (100, 200, 300),
    (UNSIGNED_32_MAX, 0, UNSIGNED_32_MAX), (1, 1, 2)])
def test_arithmetic(left, right, result):
    assert checked_add(left, right) == result


@pytest.mark.parametrize("value", [True, False, 1.0, 1.5, None, "2", [], {}, -1, 2**32, 2**100])
def test_not_unsigned_integer(value):
    with pytest.raises(OperationError):
        require_unsigned_32(value, "left")


@pytest.mark.parametrize("arguments", [None, 1, [], {}, {"left": 1}, {"right": 2},
    {"left": 1, "right": 2, "admin": True}, {"left": True, "right": 2},
    {"left": 1.0, "right": 2}, {"left": 0, "right": -1}])
def test_bad_arguments(arguments):
    with pytest.raises(OperationError):
        validated_arguments(arguments)


def test_all_256_eight_bit_pairs_and_boundary():
    for left, right in itertools.product(range(256), repeat=2):
        assert checked_add(left, right) == left + right
    with pytest.raises(OperationError) as error:
        checked_add(UNSIGNED_32_MAX, 1)
    assert error.value.code == "OVERFLOW"


def test_service_valid_call_trace():
    result = CalculationService(LocalAuthority.calculator_only()).execute("checked_add", {"left": 100, "right": 200})
    assert result.to_dict() == {"ok": True, "value": 300, "error_code": None,
        "states": ["received", "validated", "authorized", "running", "completed"]}


def test_service_denies_even_read_tool_without_authority():
    service = CalculationService(LocalAuthority(frozenset()))
    result = service.execute("checked_add", {"left": 1, "right": 2})
    assert result.error_code == "NOT_AUTHORIZED"
    assert result.states == ("received", "validated", "rejected")


def test_authority_not_accepted_as_tool_argument():
    result = CalculationService(LocalAuthority(frozenset())).execute(
        "checked_add", {"left": 1, "right": 2, "permission": True})
    assert result.error_code == "INVALID_ARGUMENT"


def test_overflow_fails_after_authorization():
    result = CalculationService(LocalAuthority.calculator_only()).execute(
        "checked_add", {"left": UNSIGNED_32_MAX, "right": 1})
    assert result.error_code == "OVERFLOW"
    assert result.states[-1] == "failed"


def test_unknown_tool():
    assert CalculationService(LocalAuthority.calculator_only()).execute(
        "delete_file", {}).error_code == "UNKNOWN_TOOL"


def test_seeded_full_unsigned_range():
    import random
    rng = random.Random(20261123)
    for _ in range(10000):
        left, right = rng.getrandbits(32), rng.getrandbits(32)
        mathematically_exact = left + right  # Python has unbounded integers.
        if mathematically_exact > UNSIGNED_32_MAX:
            with pytest.raises(OperationError) as error:
                checked_add(left, right)
            assert error.value.code == "OVERFLOW"
        else:
            assert checked_add(left, right) == mathematically_exact


def test_strict_integer_validation_with_pydantic():
    from pydantic import BaseModel, StrictInt, ValidationError
    class Arguments(BaseModel):
        left: StrictInt
        right: StrictInt
    assert Arguments(left=1, right=2).left == 1
    for invalid in (True, 1.0, "1"):
        with pytest.raises(ValidationError):
            Arguments(left=invalid, right=2)
