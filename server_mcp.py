"""Official MCP Python SDK v2 adapter (low-level Server, stdio transport).

This lower-level API is intentional. The higher-level MCPServer dynamically
builds an argument model whose default configuration may ignore extra keys.
Our contract requires *exactly* left and right; this adapter delegates the
entire untrusted argument map to the already-tested strict execution core.

Machine contracts:
  MCP-001: wire framing and version negotiation are owned by official SDK.
  INPUT-001: reject unknown tool, missing/extra fields, bool, float, overflow.
  AUTHORIZATION-001: authorization comes only from host-created authority.
  STATE-001: only the shared ToolRuntime may start/finish a computation.
  ADAPTER-001: tool schemas, validation, authorization and result checks come
               from verified_execution.runtime; this module only translates.
  OUTPUT-001: the structured result contains the exact integer result.

State: received -> validated -> authorized -> running -> completed
       received/validated -> rejected, running -> failed.
"""
from __future__ import annotations

import json
import logging
import os
import select
from time import monotonic_ns
from uuid import UUID, uuid4
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

from verified_execution.core import LOCAL_PUBLISHER, LocalAuthority
from verified_execution.durable import checked_add_recorded_spec
from verified_execution.runtime import ToolOutcome, ToolRuntime
from verified_execution.tools import ToolRegistry, ToolSpec, load_registry, thaw
from verified_execution.ledger import HostPrincipal, OperationLedger
from verified_execution.worker_leases import LeasedLedger, LeasedToolBridge
from verified_execution.cancellable_bridge import CancellableLeasedToolBridge, Checkpoint
from verified_execution.cancellation_journal import CancellationJournal
from verified_execution.recorded_tool_bridge import RECORDED_TOOL_NAME, RecordedToolBridge
from verified_execution.request_limits import ReadOnlyPolicy
from verified_execution.policy_config import load_local_policy

ROOT = Path(__file__).resolve().parent
# POLICY-010/011/012: default host-owned deployment policy, not client arguments.
_LIMITS = load_local_policy(ROOT / "config" / "limits.json")
_READONLY_POLICY = ReadOnlyPolicy(
    timeout_ms=_LIMITS.readonly_request_timeout_ms,
    max_concurrent=_LIMITS.readonly_max_concurrent,
)
REGISTRY = load_registry(ROOT / "contracts")
CAPABILITIES = json.loads((ROOT / "contracts" / "local_capabilities.json").read_text(encoding="utf-8"))
ERROR_CATEGORY = {code: category for category, codes in CAPABILITIES["error_categories"].items()
                  for code in codes}


def to_mcp_tool(spec: ToolSpec) -> Tool:
    """Translate a protocol-independent ToolSpec into an MCP Tool."""
    contract = {"publisher": LOCAL_PUBLISHER}
    if spec.contract_version is not None:
        contract["version"] = spec.contract_version
    return Tool(name=spec.name, description=spec.description,
                meta={"io.github.anionix/contract": contract},
                input_schema=thaw(spec.input_schema), output_schema=thaw(spec.output_schema))


TOOL = to_mcp_tool(REGISTRY.get("checked_add"))
INPUT_SCHEMA = TOOL.input_schema
OUTPUT_SCHEMA = TOOL.output_schema




def _tool_error(code: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=code)], is_error=True)


def _reply(outcome: ToolOutcome) -> CallToolResult:
    """Translate a verified ToolOutcome into an MCP result. No checks happen here."""
    if not outcome.ok:
        return _tool_error(outcome.error_code or "EXECUTION_FAILED")
    return CallToolResult(content=[TextContent(type="text", text=str(outcome.value))],
                          structured_content=outcome.structured(),
                          meta={"io.github.anionix/source": "local_calculator"})


def build_server(authority: LocalAuthority | None = None, *,
                 registry: ToolRegistry | None = None,
                 ledger_path: str | None = None,
                 principal: str = "local-process",
                 leased: bool = True,
                 call_budget: int | None = None,
                 before_dispatch: Checkpoint | None = None,
                 after_dispatch: Checkpoint | None = None,
                 after_commit: Checkpoint | None = None) -> Server:
    """Host chooses principal, authority and database. Never use caller-provided identity."""
    identity = HostPrincipal(principal)
    # task.authorization.002: ledger subject and registered service are separate identities.
    if identity.name == LOCAL_PUBLISHER:
        raise ValueError("principal must differ from the registered service")
    if call_budget is not None and (type(call_budget) is not int or call_budget < 0):
        raise ValueError("call budget must be a nonnegative integer")
    remaining_calls = call_budget
    if authority is None:
        authority = LocalAuthority.calculator_only()
    registry = registry if registry is not None else REGISTRY
    durable = {}
    if ledger_path is not None:
        if leased:
            # CANCEL-DURABLE-002: startup replay is host-owned, before tools
            # become available. An unrecoverable journal error fails startup.
            ledger = LeasedLedger(ledger_path)
            intent_path = Path(ledger_path).with_name(Path(ledger_path).name + '.cancel-intents')
            journal = CancellationJournal(intent_path)
            journal.replay(ledger)
            bridge = CancellableLeasedToolBridge(
                ledger, principal=identity.name, authority=authority,
                before_dispatch=before_dispatch, after_dispatch=after_dispatch,
                after_commit=after_commit, cancellation_journal=journal)
        else:
            # Local legacy mode only; don't mix with leased workers in production.
            # No request-scoped cancellation on this path.
            bridge = RecordedToolBridge(OperationLedger(ledger_path),
                                        principal=identity.name, authority=authority)
        # The optional recorded tool is a registry tool like any other; the host
        # attaches the ledger executor that performs it.
        if RECORDED_TOOL_NAME not in registry:
            registry = registry.with_tools(checked_add_recorded_spec(ROOT / "contracts"))
        durable[RECORDED_TOOL_NAME] = bridge
    # POLICY-011: the host's read-only time and concurrency budget.
    runtime = ToolRuntime(authority, registry, durable=durable, readonly_policy=_READONLY_POLICY)
    tools = [to_mcp_tool(spec) for spec in runtime.tools()]

    async def list_tools(_context: ServerRequestContext, _params: PaginatedRequestParams | None) -> ListToolsResult:
        return ListToolsResult(tools=list(tools))

    async def execute_tool(params: CallToolRequestParams) -> CallToolResult:
        # The SDK owns request identity, notification dispatch and
        # post-cancellation response suppression; cancelling this coroutine
        # reaches the durable executor's cancellation checkpoints.
        return _reply(await runtime.acall(params.name, params.arguments))

    trace_id = str(uuid4())

    async def call_tool(_context: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
        # task.contracts.005 / .008: one host trace per server and one attempt per delivery.
        # Only host-generated identifiers and fixed status values cross the audit boundary.
        event = {"source": "mcp.tools.call", "trace_id": trace_id, "attempt_id": str(uuid4()),
                 "outcome": "interrupted"}
        started = monotonic_ns()
        nonlocal remaining_calls
        try:
            # task.authorization.018: debit before dispatch, with no await between check and debit.
            # Each registered call attempt consumes one unit, including retries and failures.
            registered = runtime.serves(params.name)
            if registered and remaining_calls == 0:
                reply = _tool_error("BUDGET_EXCEEDED")
            else:
                if registered and remaining_calls is not None:
                    remaining_calls -= 1
                reply = await execute_tool(params)
            if getattr(reply, "is_error", False):
                code = reply.content[0].text
                event["outcome"] = "denied" if code in {"NOT_AUTHORIZED", "BUDGET_EXCEEDED"} else "rejected"
                event["error_category"] = ERROR_CATEGORY.get(code, "unknown")
            else:
                event["outcome"] = "succeeded"
                identifier = reply.structured_content.get("operation_id")
                if identifier is not None:
                    # The durable ledger issues UUIDs. Never log arbitrary strings from a worker.
                    try:
                        event["operation_id"] = str(UUID(identifier))
                    except (TypeError, ValueError, AttributeError):
                        pass
            return reply
        finally:
            event["elapsed_ns"] = monotonic_ns() - started
            # A slow or disconnected diagnostics pipe must not stop protocol replies.
            try:
                if select.select([], [2], [], 0)[1]:
                    os.write(2, (json.dumps(event, separators=(",", ":")) + "\n").encode("ascii"))
            except OSError:
                pass

    return Server("verified-calculator", version="0.11.0", on_list_tools=list_tools, on_call_tool=call_tool)


# The optional recorded tool is enabled only by a trusted host-side environment.
# Never obtain the database path or principal from MCP tool arguments.
server = build_server(ledger_path=os.environ.get("VERIFIED_EXECUTION_LEDGER_PATH"))


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    logging.getLogger("mcp").addHandler(logging.NullHandler())
    logging.getLogger("mcp").propagate = False
    anyio.run(main)
