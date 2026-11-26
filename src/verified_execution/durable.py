"""Durable (ledger-backed) tools on the shared registry.

A durable tool is declared with a ToolSpec like any other tool, but its
work is done by a host-attached executor that reserves, dispatches and
records the operation in the ledger (RecordedToolBridge or
CancellableLeasedToolBridge). ToolRuntime.acall validates, authorizes,
delegates to that executor and verifies the result before release.

Machine contracts:
  DURABLE-001: the caller controls request_key; never principal or grants.
  DURABLE-002: exactly request_key, left and right; same error codes and
               order as the ledger bridges (INVALID_ARGUMENT, then
               INVALID_REQUEST_KEY, then operand errors).
  DURABLE-003: a durable spec is never run directly; only its executor runs.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NamedTuple

from .core import OperationError, UNSIGNED_32_MAX, require_unsigned_32, validated_arguments
from .recorded_tool_bridge import RECORDED_TOOL_NAME
from .tools import DEFAULT_CONTRACTS_DIR, ToolSpec

MAX_REQUEST_KEY_BYTES = 256


class RecordedCall(NamedTuple):
    request_key: str
    left: int
    right: int


def validate_request_key(key: Any) -> str:
    """A client-chosen idempotency key: non-empty, no NUL, at most 256 UTF-8 bytes."""
    if type(key) is not str or not key or "\x00" in key:
        raise OperationError("INVALID_REQUEST_KEY", "invalid request key")
    try:
        encoded = key.encode("utf-8")
    except UnicodeError:
        raise OperationError("INVALID_REQUEST_KEY", "invalid request key") from None
    if len(encoded) > MAX_REQUEST_KEY_BYTES:
        raise OperationError("INVALID_REQUEST_KEY", "request key too long")
    return key


def validate_recorded(arguments: Any) -> RecordedCall:
    if type(arguments) is not dict or set(arguments) != {"request_key", "left", "right"}:
        raise OperationError("INVALID_ARGUMENT", "arguments require only request_key, left and right")
    key = validate_request_key(arguments["request_key"])
    left, right = validated_arguments({"left": arguments["left"], "right": arguments["right"]})
    return RecordedCall(key, left, right)


def _verify_recorded(call: RecordedCall, value: Any) -> None:
    left = require_unsigned_32(call.left, "left")
    right = require_unsigned_32(call.right, "right")
    if type(value) is not int or not 0 <= value <= UNSIGNED_32_MAX or value - left != right:
        raise OperationError("INVALID_TOOL_RESULT", "invalid calculation result")


def _durable_only(_call: RecordedCall) -> None:
    # DURABLE-003: ToolRuntime never calls this; the ledger executor does the work.
    raise OperationError("UNSUPPORTED_OPERATION", "tool requires the durable operation path")


def checked_add_recorded_spec(contracts_dir: Path = DEFAULT_CONTRACTS_DIR) -> ToolSpec:
    def read(name: str) -> Any:
        return json.loads((contracts_dir / name).read_text(encoding="utf-8"))

    output_contract = read("checked_add_recorded.output.contract.json")
    success = output_contract["domains"]["success"]
    return ToolSpec(
        name=RECORDED_TOOL_NAME,
        description="Durable, strict unsigned 32-bit addition; requires stable request_key.",
        risk=read("tool_risks.json")["tools"][RECORDED_TOOL_NAME],
        input_schema=read("checked_add_recorded.input.schema.json"),
        output_schema={
            "type": success["type"],
            "properties": success["properties"],
            "required": success["required"],
            "additionalProperties": False,
        },
        validate=validate_recorded,
        run=_durable_only,
        verify_result=_verify_recorded,
        contract_version=output_contract["contract_version"],
        # The durable record of an addition is permitted by the addition grant,
        # as the ledger bridges and LeasedLedger.start already require.
        grant="checked_add",
    )
