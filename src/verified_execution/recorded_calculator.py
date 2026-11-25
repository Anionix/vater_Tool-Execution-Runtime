"""Integration of trusted calculator authorization with durable operation ledger.

INTEGRATION-001: Untrusted tool arguments never supply the principal identity.
INTEGRATION-002: Validation and host policy happen *before* ledger dispatch.
INTEGRATION-003: The ledger dispatch is committed BEFORE calculator execution.
INTEGRATION-004: Replays never cause a second execution in this adapter.
INTEGRATION-005: This demo operates on a PURE calculator, not a mutation tool.

This is not the official MCP adapter: it proves a Python application flow only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .core import LocalAuthority, OperationError, checked_add, validated_arguments
from .ledger import LedgerError, OperationLedger


@dataclass(frozen=True)
class RecordedResult:
    ok: bool
    value: int | None
    error_code: str | None
    operation_id: str | None
    ledger_state: str | None
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "value": self.value,
            "error_code": self.error_code,
            "operation_id": self.operation_id, "ledger_state": self.ledger_state,
            "replayed": self.replayed,
        }


class RecordedCalculator:
    """The principal and permissions are configured by the host, not the client."""
    def __init__(self, ledger: OperationLedger, *, principal: str,
                 authority: LocalAuthority):
        self.ledger = ledger
        self.principal = principal
        self.authority = authority

    def execute(self, *, request_key: str, name: str, arguments: Any) -> RecordedResult:
        # INTEGRATION-001: this method never accepts a caller-supplied principal.
        try:
            if name != "checked_add":
                raise OperationError("UNKNOWN_TOOL", "tool not registered")
            left, right = validated_arguments(arguments)
        except OperationError as exc:
            return RecordedResult(False, None, exc.code, None, None)

        # INTEGRATION-002: never reserve or dispatch an unauthorized tool.
        if not self.authority.permits(name):
            return RecordedResult(False, None, "NOT_AUTHORIZED", None, None)

        try:
            op = self.ledger.reserve(
                principal=self.principal, request_key=request_key,
                tool_name=name, target_id=None, resource_version=None,
                arguments={"left": left, "right": right}, requires_approval=False)
            if op.replayed:
                # REPLAY-002: never redispatch. Return only committed, principal-scoped
                # successful output. Old deployments have no saved output and fail closed.
                saved = self.ledger.read_calculation(op.operation_id, principal=self.principal)
                if not self.authority.permits(name):
                    return RecordedResult(False, None, "NOT_AUTHORIZED", None, None)
                if saved is not None:
                    return RecordedResult(True, saved, None, op.operation_id,
                                          "succeeded", replayed=True)
                # REPLAY-003: make intermediate and indeterminate states explicit.
                # A replay is not an instruction to redispatch a side effect.
                code = {"prepared": "IN_PROGRESS", "dispatched": "IN_PROGRESS",
                        "unknown": "OUTCOME_UNKNOWN"}.get(
                    op.state, "ALREADY_RECORDED")
                return RecordedResult(False, None, code, op.operation_id,
                                      op.state, replayed=True)
            self.ledger.begin_dispatch(op.operation_id)
        except LedgerError as exc:
            return RecordedResult(False, None, exc.code, None, None)

        # INTEGRATION-003: the dispatch commitment is durable before doing work.
        try:
            total = checked_add(left, right)
        except OperationError as exc:
            self.ledger.record_definitive_result(op.operation_id, "failed")
            return RecordedResult(False, None, exc.code, op.operation_id, "failed")
        except BaseException:
            # Failure after dispatch is ambiguous unless proven otherwise.
            self.ledger.mark_unknown(op.operation_id)
            raise
        self.ledger.complete_calculation(op.operation_id, total)
        return RecordedResult(True, total, None, op.operation_id, "succeeded")
