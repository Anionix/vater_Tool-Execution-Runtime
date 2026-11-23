"""CONFIG-TEST-001: fail-closed settings are independently tested without MCP."""
import json
import pytest
from verified_execution.policy_config import (
    LocalPolicyConfiguration, PolicyConfigurationError, load_local_policy,
    validate_local_policy,
)

GOOD = {
    "policy_version": "1.0.0",
    "request_input_max_bytes": 4096,
    "request_output_max_bytes": 65536,
    "json_max_depth": 32,
    "readonly_request_timeout_ms": 2000,
    "readonly_max_concurrent": 1,
    "scope": "single-local-user; pure checked_add only",
    "status": "prototype; official MCP SDK validation pending",
}


def test_valid_config_is_typed_and_immutable():
    result = validate_local_policy(GOOD)
    assert isinstance(result, LocalPolicyConfiguration)
    assert result.readonly_max_concurrent == 1
    with pytest.raises(AttributeError):
        result.readonly_max_concurrent = 32


@pytest.mark.parametrize("mutation", [
    {"request_input_max_bytes": 4097},
    {"request_input_max_bytes": 0},
    {"request_input_max_bytes": True},
    {"request_input_max_bytes": "4096"},
    {"request_output_max_bytes": 65537},
    {"json_max_depth": 33},
    {"readonly_request_timeout_ms": 2001},
    {"readonly_request_timeout_ms": -1},
    {"readonly_max_concurrent": 2},
    {"readonly_max_concurrent": False},
    {"policy_version": "2.0.0"},
    {"scope": "network-enabled"},
    {"status": "production-ready"},
    {"unexpected": "allow all"},
])
def test_rejects_unsafe_or_unknown_values(mutation):
    with pytest.raises(PolicyConfigurationError):
        validate_local_policy({**GOOD, **mutation})


def test_missing_or_nonmapping_rejected():
    missing = GOOD.copy()
    missing.pop("request_input_max_bytes")
    for value in (missing, None, [], "any", 23):
        with pytest.raises(PolicyConfigurationError):
            validate_local_policy(value)


def test_file_load_success(tmp_path):
    path = tmp_path / "limits.json"
    path.write_text(json.dumps(GOOD), encoding="utf-8")
    assert load_local_policy(path).request_output_max_bytes == 65536


@pytest.mark.parametrize("raw", [
    '{"scope":1,"scope":2}',
    '{"value":NaN}',
    '{"value":Infinity}',
    '{',
    'x' * 4100,
    '[]',
])
def test_bad_json_fail_closed(tmp_path, raw):
    path = tmp_path / "limits.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(PolicyConfigurationError):
        load_local_policy(path)


def test_invalid_utf8_and_missing_file_fail_closed(tmp_path):
    path = tmp_path / "limits.json"
    path.write_bytes(b"\xff")
    with pytest.raises(PolicyConfigurationError):
        load_local_policy(path)
    with pytest.raises(PolicyConfigurationError):
        load_local_policy(tmp_path / "missing")


def test_no_raw_secret_leaked_in_error(tmp_path):
    secret = "private-user-secret-content"
    path = tmp_path / "limits.json"
    path.write_text(json.dumps({**GOOD, "secret": secret}), encoding="utf-8")
    with pytest.raises(PolicyConfigurationError) as exc:
        load_local_policy(path)
    assert secret not in str(exc.value)
