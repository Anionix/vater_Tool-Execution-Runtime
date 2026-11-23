"""Official MCP Python SDK v2 adapter (low-level Server, stdio transport).

This lower-level API is intentional. The higher-level MCPServer dynamically
builds an argument model whose default configuration may ignore extra keys.
Our contract requires *exactly* left and right; this adapter delegates the
entire untrusted argument map to the already-tested strict execution core.

Machine contracts:
  MCP-001: wire framing and version negotiation are owned by official SDK.
  INPUT-001: reject unknown tool, missing/extra fields, bool, float, overflow.
  AUTHORIZATION-001: authorization comes only from host-created authority.
  STATE-001: only the CalculationService may start/finish the computation.
  OUTPUT-001: the structured result contains the exact integer result.

State: received -> validated -> authorized -> running -> completed
       received/validated -> rejected, running -> failed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

from verified_execution.core import CalculationService, LocalAuthority
from verified_execution.ledger import OperationLedger
from verified_execution.worker_leases import LeasedLedger, LeasedToolBridge
from verified_execution.cancellable_bridge import CancellableLeasedToolBridge, Checkpoint
from verified_execution.cancellation_journal import CancellationJournal
from verified_execution.recorded_tool_bridge import RECORDED_TOOL_NAME, RecordedToolBridge
from verified_execution.request_limits import ReadOnlyPolicy, BoundedReadOnlyExecutor
from verified_execution.policy_config import load_local_policy

ROOT = Path(__file__).resolve().parent
# POLICY-010/011/012: default host-owned deployment policy, not client arguments.
_LIMITS = load_local_policy(ROOT / "config" / "limits.json")
_READONLY_POLICY = ReadOnlyPolicy(
    timeout_ms=_LIMITS.readonly_request_timeout_ms,
    max_concurrent=_LIMITS.readonly_max_concurrent,
)
INPUT_SCHEMA = json.loads((ROOT / "contracts" / "checked_add.input.schema.json").read_text(encoding="utf-8"))
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"result": {"type": "integer", "minimum": 0, "maximum": 4294967295}},
    "required": ["result"],
    "additionalProperties": False,
}
TOOL = Tool(
    name="checked_add",
    description="Add unsigned 32-bit integers without overflow or side effects.",
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
)


RECORDED_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "request_key": {"type": "string", "minLength": 1, "maxLength": 256},
        "left": INPUT_SCHEMA["properties"]["left"],
        "right": INPUT_SCHEMA["properties"]["right"],
    },
    "required": ["request_key", "left", "right"],
    "additionalProperties": False,
}
RECORDED_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "result": OUTPUT_SCHEMA["properties"]["result"],
        "operation_id": {"type": "string"},
        "replayed": {"type": "boolean"},
    },
    "required": ["result", "operation_id", "replayed"],
    "additionalProperties": False,
}
RECORDED_TOOL = Tool(
    name=RECORDED_TOOL_NAME,
    description="Durable, strict unsigned 32-bit addition; requires stable request_key.",
    input_schema=RECORDED_INPUT_SCHEMA,
    output_schema=RECORDED_OUTPUT_SCHEMA,
)


def build_server(authority: LocalAuthority | None = None, *,
                 ledger_path: str | None = None,
                 principal: str = "local-process",
                 leased: bool = True,
                 before_dispatch: Checkpoint | None = None,
                 after_dispatch: Checkpoint | None = None,
                 after_commit: Checkpoint | None = None) -> Server:
    """Host chooses principal, authority and database. Never use caller-provided identity."""
    if authority is None:
        authority = LocalAuthority.calculator_only()
    service = CalculationService(authority)
    bounded_readonly = BoundedReadOnlyExecutor(_READONLY_POLICY)
    bridge = None
    if ledger_path is not None:
        if leased:
            # CANCEL-DURABLE-002: startup replay is host-owned, before tools
            # become available. An unrecoverable journal error fails startup.
            ledger = LeasedLedger(ledger_path)
            intent_path = Path(ledger_path).with_name(Path(ledger_path).name + '.cancel-intents')
            journal = CancellationJournal(intent_path)
            journal.replay(ledger)
            bridge = CancellableLeasedToolBridge(
                ledger, principal=principal, authority=authority,
                before_dispatch=before_dispatch, after_dispatch=after_dispatch,
                after_commit=after_commit, cancellation_journal=journal)
        else:
            # Local legacy mode only; don't mix with leased workers in production.
            bridge = RecordedToolBridge(OperationLedger(ledger_path),
                                        principal=principal,authority=authority)

    async def list_tools(_context: ServerRequestContext, _params: PaginatedRequestParams | None) -> ListToolsResult:
        return ListToolsResult(tools=[TOOL, RECORDED_TOOL] if bridge is not None else [TOOL])

    async def call_tool(_context: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
        if bridge is not None and params.name == RECORDED_TOOL_NAME:
            if isinstance(bridge, CancellableLeasedToolBridge):
                # The SDK owns request identity, notification dispatch and
                # post-cancellation response suppression. This coroutine has
                # actual cancellation checkpoints and stores state separately.
                outcome_recorded = await bridge.acall(params.name, params.arguments)
            else:
                # Local migration path only, without request-scoped cancellation.
                outcome_recorded = bridge.call(params.name, params.arguments)
            if outcome_recorded.ok:
                assert outcome_recorded.value is not None and outcome_recorded.operation_id is not None
                return CallToolResult(
                    content=[TextContent(type="text", text=str(outcome_recorded.value))],
                    structured_content={"result": outcome_recorded.value,
                                        "operation_id": outcome_recorded.operation_id,
                                        "replayed": outcome_recorded.replayed})
            return CallToolResult(
                content=[TextContent(type="text", text=outcome_recorded.error_code or "EXECUTION_FAILED")],
                is_error=True)
        # POLICY-011: bounded *elapsed response time* for the pure calculator.
        # No untrusted code or external side effects execute in this branch.
        try:
            outcome = await bounded_readonly.run(service.execute, params.name, params.arguments)
        except TimeoutError:
            return CallToolResult(
                content=[TextContent(type="text", text="TIMEOUT")], is_error=True)
        if outcome.succeeded:
            assert outcome.result is not None
            return CallToolResult(
                content=[TextContent(type="text", text=str(outcome.result))],
                structured_content={"result": outcome.result},
            )
        # Machine-readable, stable, non-sensitive failure classification.
        return CallToolResult(
            content=[TextContent(type="text", text=outcome.error_code or "EXECUTION_FAILED")],
            is_error=True,
        )

    return Server("verified-calculator", version="0.11.0", on_list_tools=list_tools, on_call_tool=call_tool)


# The optional recorded tool is enabled only by a trusted host-side environment.
# Never obtain the database path or principal from MCP tool arguments.
import os
server = build_server(ledger_path=os.environ.get("VERIFIED_EXECUTION_LEDGER_PATH"))


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
