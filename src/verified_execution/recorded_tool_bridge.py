"""Offline-testable MCP application adapter; no SDK objects are imported.

Machine contracts:
  BRIDGE-001: the caller controls request_key but never principal or permissions.
  BRIDGE-002: only exactly request_key, left, right are accepted.
  BRIDGE-003: all execution goes through RecordedCalculator authorization and ledger.
  BRIDGE-004: successful replay comes from the durable result, never re-execution.

The request key identifies a client-intended logical operation. JSON-RPC request
IDs are not globally stable across new sessions and MUST NOT be used as durable
idempotency keys. Host sets identity by trusted local process configuration.
"""
from __future__ import annotations
from dataclasses import dataclass
from functools import lru_cache
import json
from typing import Any

from .core import LocalAuthority
from .ledger import OperationLedger
from .recorded_calculator import RecordedCalculator, RecordedResult

RECORDED_TOOL_NAME = "checked_add_recorded"

@dataclass(frozen=True)
class BridgeResult:
    ok: bool
    value: int | None
    operation_id: str | None
    replayed: bool
    error_code: str | None


@lru_cache(maxsize=None)
def declared_failures(tool_name: str) -> frozenset[str]:
    """The failure codes a tool's output contract declares (domains.failure)."""
    from .tools import DEFAULT_CONTRACTS_DIR
    contract = json.loads((DEFAULT_CONTRACTS_DIR / f"{tool_name}.output.contract.json").read_text(encoding="utf-8"))
    codes = contract["domains"]["failure"]["error_code"]["enum"]
    if not codes or not all(type(code) is str and code for code in codes) or "INVALID_TOOL_RESULT" not in codes:
        raise ValueError(f"invalid failure codes in the {tool_name} output contract")
    return frozenset(codes)


def within_contract(result: BridgeResult, declared: frozenset[str]) -> BridgeResult:
    """CONTRACT-FAILURE-001: a durable executor's reply is part of the tool's
    response contract. A failure code the contract does not declare (an
    internal ledger condition, for example) is never passed to a client: it
    becomes INVALID_TOOL_RESULT. Successful replies are verified by ToolRuntime."""
    if result.ok or result.error_code in declared:
        return result
    return BridgeResult(False, None, result.operation_id, result.replayed, "INVALID_TOOL_RESULT")


class RecordedToolBridge:
    def __init__(self, ledger: OperationLedger, *, principal: str, authority: LocalAuthority):
        self._service = RecordedCalculator(ledger, principal=principal, authority=authority)

    def call(self, name: str, arguments: Any) -> BridgeResult:
        return within_contract(self._call(name, arguments), declared_failures(RECORDED_TOOL_NAME))

    def _call(self, name: str, arguments: Any) -> BridgeResult:
        if name != RECORDED_TOOL_NAME:
            return BridgeResult(False, None, None, False, "UNKNOWN_TOOL")
        if type(arguments) is not dict or set(arguments) != {"request_key", "left", "right"}:
            return BridgeResult(False, None, None, False, "INVALID_ARGUMENT")
        key = arguments["request_key"]
        if type(key) is not str or not key or "\x00" in key:
            return BridgeResult(False, None, None, False, "INVALID_REQUEST_KEY")
        try:
            key_bytes = key.encode("utf-8")
        except UnicodeError:
            return BridgeResult(False, None, None, False, "INVALID_REQUEST_KEY")
        if len(key_bytes) > 256:
            return BridgeResult(False, None, None, False, "INVALID_REQUEST_KEY")
        outcome: RecordedResult = self._service.execute(
            request_key=key, name="checked_add",
            arguments={"left": arguments["left"], "right": arguments["right"]})
        return BridgeResult(outcome.ok, outcome.value, outcome.operation_id,
                            outcome.replayed, outcome.error_code)
