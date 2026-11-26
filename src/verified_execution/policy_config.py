"""Strict host-owned local limits.

Machine state: RAW -> PARSED -> VALIDATED -> ACCEPTED | REJECTED.
CONFIG-001: reject unknown/missing settings, coercion and bool-as-int.
CONFIG-002: reject unbounded settings before starting the MCP server.
CONFIG-003: never disclose configuration contents in error text.
This module is not user authentication: only the host selects the path.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .json_bounds import check_json_depth, WireInputRejected

EXPECTED_FIELDS = frozenset({
    "policy_version", "request_input_max_bytes", "request_output_max_bytes",
    "json_max_depth", "readonly_request_timeout_ms", "readonly_max_concurrent",
    "scope", "status",
})


class PolicyConfigurationError(ValueError):
    """Stable fail-closed configuration error."""


@dataclass(frozen=True)
class LocalPolicyConfiguration:
    policy_version: str
    request_input_max_bytes: int
    request_output_max_bytes: int
    json_max_depth: int
    readonly_request_timeout_ms: int
    readonly_max_concurrent: int
    scope: str
    status: str


def _bounded_int(raw: dict[str, Any], key: str, lower: int, upper: int) -> int:
    value = raw[key]
    if type(value) is not int or not lower <= value <= upper:
        raise PolicyConfigurationError("INVALID_" + key.upper())
    return value


def validate_local_policy(raw: Any) -> LocalPolicyConfiguration:
    """Exact schema, bounded budgets, no silent defaults or coercion."""
    if type(raw) is not dict or set(raw) != EXPECTED_FIELDS:
        raise PolicyConfigurationError("INVALID_POLICY_FIELDS")
    if type(raw["policy_version"]) is not str or raw["policy_version"] != "1.0.0":
        raise PolicyConfigurationError("INVALID_POLICY_VERSION")
    if type(raw["scope"]) is not str or raw["scope"] != "single-local-user; pure checked_add only":
        raise PolicyConfigurationError("INVALID_SCOPE")
    if type(raw["status"]) is not str or raw["status"] != "prototype; official MCP SDK validation pending":
        raise PolicyConfigurationError("INVALID_STATUS")
    return LocalPolicyConfiguration(
        policy_version=raw["policy_version"],
        request_input_max_bytes=_bounded_int(raw, "request_input_max_bytes", 128, 4096),
        request_output_max_bytes=_bounded_int(raw, "request_output_max_bytes", 128, 65536),
        json_max_depth=_bounded_int(raw, "json_max_depth", 1, 32),
        readonly_request_timeout_ms=_bounded_int(raw, "readonly_request_timeout_ms", 1, 2000),
        readonly_max_concurrent=_bounded_int(raw, "readonly_max_concurrent", 1, 1),
        scope=raw["scope"], status=raw["status"],
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PolicyConfigurationError("DUPLICATE_POLICY_FIELD")
        result[key] = value
    return result


def _reject_nonfinite(_constant: str) -> None:
    raise PolicyConfigurationError("INVALID_POLICY_NUMBER")


def load_local_policy(path: str | Path) -> LocalPolicyConfiguration:
    """Load a host-selected file; reject duplicates, large files and invalid JSON."""
    try:
        with open(path, "rb") as source:
            data = source.read(4097)
        if len(data) > 4096:
            raise PolicyConfigurationError("POLICY_TOO_LARGE")
        text = data.decode("utf-8", errors="strict")
        check_json_depth(text, 1)
        raw = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, WireInputRejected) as exc:
        raise PolicyConfigurationError("INVALID_POLICY_FILE") from exc
    return validate_local_policy(raw)
