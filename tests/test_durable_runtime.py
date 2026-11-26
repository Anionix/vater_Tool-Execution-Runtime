"""The recorded (ledger-backed) tool on the shared registry and runtime."""
import asyncio
from dataclasses import dataclass, replace
import json
from pathlib import Path
from threading import Event

import pytest

from adapter_drivers import load_mcp_adapter, with_sdk_client
from verified_execution.cancellable_bridge import CancellableLeasedToolBridge
from verified_execution.core import LocalAuthority
from verified_execution.durable import checked_add_recorded_spec, validate_recorded
from verified_execution.ledger import OperationLedger
from verified_execution.recorded_tool_bridge import RECORDED_TOOL_NAME, RecordedToolBridge
from verified_execution.runtime import ToolRuntime
from verified_execution.tools import default_registry, thaw
from verified_execution.worker_leases import LeasedLedger

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
ADD = LocalAuthority(frozenset({"checked_add"}))
RECORDED = default_registry().with_tools(checked_add_recorded_spec())

INVALID_CALLS = [
    ({"left": 1, "right": 2}, "INVALID_ARGUMENT"),
    ({"request_key": "k", "left": 1, "right": 2, "extra": 0}, "INVALID_ARGUMENT"),
    ([], "INVALID_ARGUMENT"),
    ({"request_key": True, "left": 1, "right": 2}, "INVALID_REQUEST_KEY"),
    ({"request_key": "", "left": 1, "right": 2}, "INVALID_REQUEST_KEY"),
    ({"request_key": "a\x00b", "left": 1, "right": 2}, "INVALID_REQUEST_KEY"),
    ({"request_key": "x" * 257, "left": 1, "right": 2}, "INVALID_REQUEST_KEY"),
    ({"request_key": "\ud800", "left": 1, "right": 2}, "INVALID_REQUEST_KEY"),
    ({"request_key": "k", "left": True, "right": 2}, "INVALID_ARGUMENT"),
    ({"request_key": "k", "left": 1.0, "right": 2}, "INVALID_ARGUMENT"),
    ({"request_key": "k", "left": -1, "right": 2}, "OUT_OF_RANGE"),
]


def run(coroutine):
    return asyncio.run(coroutine)


def legacy_runtime(tmp_path, authority=ADD):
    bridge = RecordedToolBridge(OperationLedger(tmp_path / "ledger.sqlite"), principal="local-user",
                                authority=authority)
    return ToolRuntime(authority, RECORDED, durable={RECORDED_TOOL_NAME: bridge})


def leased_runtime(tmp_path, authority=ADD, **checkpoints):
    bridge = CancellableLeasedToolBridge(LeasedLedger(tmp_path / "leased.sqlite"), principal="local-user",
                                         authority=authority, **checkpoints)
    return ToolRuntime(authority, RECORDED, durable={RECORDED_TOOL_NAME: bridge})


# --- the spec -------------------------------------------------------------

def test_recorded_spec_comes_from_contract_files():
    spec = checked_add_recorded_spec()
    risks = json.loads((CONTRACTS / "tool_risks.json").read_text())["tools"]
    output = json.loads((CONTRACTS / "checked_add_recorded.output.contract.json").read_text())
    assert thaw(spec.input_schema) == json.loads((CONTRACTS / "checked_add_recorded.input.schema.json").read_text())
    assert thaw(spec.output_schema)["properties"] == output["domains"]["success"]["properties"]
    assert spec.risk == risks[RECORDED_TOOL_NAME] == "state_change"
    assert spec.grant == "checked_add"


def test_recorded_schema_keeps_the_published_shape():
    schema = thaw(checked_add_recorded_spec().input_schema)
    add = json.loads((CONTRACTS / "checked_add.input.schema.json").read_text())
    assert schema["required"] == ["request_key", "left", "right"] and schema["additionalProperties"] is False
    assert schema["properties"]["request_key"] == {"type": "string", "minLength": 1, "maxLength": 256}
    assert {key: schema["properties"][key] for key in ("left", "right")} == add["properties"]
    output = thaw(checked_add_recorded_spec().output_schema)
    assert output["required"] == ["result", "operation_id", "replayed"]
    assert output["additionalProperties"] is False


@pytest.mark.parametrize("arguments,code", INVALID_CALLS, ids=range(len(INVALID_CALLS)))
def test_validation_matches_both_ledger_bridges(tmp_path, arguments, code):
    from verified_execution.core import OperationError
    with pytest.raises(OperationError) as error:
        validate_recorded(arguments)
    assert error.value.code == code
    legacy = RecordedToolBridge(OperationLedger(tmp_path / "a.sqlite"), principal="u", authority=ADD)
    leased = CancellableLeasedToolBridge(LeasedLedger(tmp_path / "b.sqlite"), principal="u", authority=ADD)
    assert legacy.call(RECORDED_TOOL_NAME, arguments).error_code == code
    assert run(leased.acall(RECORDED_TOOL_NAME, arguments)).error_code == code


# --- runtime --------------------------------------------------------------

def test_durable_tool_is_served_only_with_an_executor(tmp_path):
    assert [spec.name for spec in ToolRuntime(ADD, RECORDED).tools()] == ["checked_add"]
    assert run(ToolRuntime(ADD, RECORDED).acall(RECORDED_TOOL_NAME, {"request_key": "k", "left": 1, "right": 2})) \
        .error_code == "UNSUPPORTED_OPERATION"
    assert [spec.name for spec in legacy_runtime(tmp_path).tools()] == ["checked_add", RECORDED_TOOL_NAME]


def test_executor_cannot_be_bound_to_other_risk_classes(tmp_path):
    from verified_execution.tools import ToolRegistry
    bridge = object()
    with pytest.raises(ValueError, match="read_only"):
        ToolRuntime(ADD, RECORDED, durable={"checked_add": bridge})
    irreversible = ToolRegistry([replace(checked_add_recorded_spec(), name="drop", risk="irreversible")])
    with pytest.raises(ValueError, match="irreversible"):
        ToolRuntime(ADD, irreversible, durable={"drop": bridge})


@pytest.mark.parametrize("make", [legacy_runtime, leased_runtime], ids=["legacy", "leased"])
def test_durable_call_records_and_replays(tmp_path, make):
    runtime = make(tmp_path)
    arguments = {"request_key": "first", "left": 40, "right": 2}
    first = run(runtime.acall(RECORDED_TOOL_NAME, arguments))
    again = run(runtime.acall(RECORDED_TOOL_NAME, arguments))
    assert (first.ok, first.value, first.replayed) == (True, 42, False)
    assert (again.ok, again.value, again.replayed, again.operation_id) == (True, 42, True, first.operation_id)
    assert first.structured() == {"result": 42, "operation_id": first.operation_id, "replayed": False}


@pytest.mark.parametrize("make", [legacy_runtime, leased_runtime], ids=["legacy", "leased"])
def test_durable_call_requires_the_addition_grant(tmp_path, make):
    runtime = make(tmp_path, authority=LocalAuthority(frozenset({RECORDED_TOOL_NAME})))
    outcome = run(runtime.acall(RECORDED_TOOL_NAME, {"request_key": "k", "left": 1, "right": 2}))
    assert outcome.error_code == "NOT_AUTHORIZED"


def test_durable_overflow_is_a_recorded_failure(tmp_path):
    outcome = run(leased_runtime(tmp_path).acall(RECORDED_TOOL_NAME,
                                                {"request_key": "k", "left": 2**32 - 1, "right": 1}))
    assert (outcome.ok, outcome.error_code) == (False, "OVERFLOW")


@dataclass
class Reply:
    ok: bool
    value: object
    operation_id: object
    replayed: object
    error_code: object = None


class FakeExecutor:
    """Stands in for a ledger bridge that returns a bad or forged reply."""
    def __init__(self, reply):
        self.reply = reply

    async def acall(self, _name, _arguments):
        return self.reply


@pytest.mark.parametrize("reply", [
    Reply(True, 6, "op-1", False),          # wrong sum
    Reply(True, 5.0, "op-1", False),        # float
    Reply(True, True, "op-1", False),       # boolean
    Reply(True, 5, None, False),            # missing operation id
    Reply(True, 5, "", False),              # empty operation id
    Reply(True, 5, "op-1", "no"),           # replayed is not boolean
], ids=["wrong_sum", "float", "boolean", "no_id", "empty_id", "replayed_text"])
def test_runtime_verifies_executor_replies(reply):
    runtime = ToolRuntime(ADD, RECORDED, durable={RECORDED_TOOL_NAME: FakeExecutor(reply)})
    outcome = run(runtime.acall(RECORDED_TOOL_NAME, {"request_key": "k", "left": 2, "right": 3}))
    assert (outcome.ok, outcome.value, outcome.error_code) == (False, None, "INVALID_TOOL_RESULT")


def test_runtime_does_not_call_the_executor_for_rejected_requests():
    calls = []

    class Recording(FakeExecutor):
        async def acall(self, name, arguments):
            calls.append(name)
            return await super().acall(name, arguments)

    executor = Recording(Reply(True, 3, "op-1", False))
    denied = ToolRuntime(LocalAuthority(frozenset()), RECORDED, durable={RECORDED_TOOL_NAME: executor})
    allowed = ToolRuntime(ADD, RECORDED, durable={RECORDED_TOOL_NAME: executor})
    assert run(denied.acall(RECORDED_TOOL_NAME, {"request_key": "k", "left": 1, "right": 2})).error_code \
        == "NOT_AUTHORIZED"
    assert run(allowed.acall(RECORDED_TOOL_NAME, {"request_key": "", "left": 1, "right": 2})).error_code \
        == "INVALID_REQUEST_KEY"
    assert calls == []


def test_grant_withdrawn_after_commit_suppresses_the_value(tmp_path):
    revoked = Event()
    authority = LocalAuthority(frozenset({"checked_add"}), revocation_event=revoked)

    async def after_commit(_operation):
        revoked.set()

    runtime = leased_runtime(tmp_path, authority=authority, after_commit=after_commit)
    outcome = run(runtime.acall(RECORDED_TOOL_NAME, {"request_key": "k", "left": 2, "right": 3}))
    assert (outcome.ok, outcome.value, outcome.error_code) == (False, None, "NOT_AUTHORIZED")


# --- MCP only translates --------------------------------------------------

def test_mcp_publishes_the_recorded_tool_from_the_registry(tmp_path):
    pytest.importorskip("mcp")
    server = load_mcp_adapter("server_mcp_durable").build_server(ADD, ledger_path=str(tmp_path / "h.sqlite"))

    async def scenario(client):
        listed = (await client.list_tools()).tools
        first = await client.call_tool(RECORDED_TOOL_NAME, {"request_key": "a", "left": 2, "right": 3})
        again = await client.call_tool(RECORDED_TOOL_NAME, {"request_key": "a", "left": 2, "right": 3})
        return listed, first, again

    listed, first, again = with_sdk_client(server, scenario)
    assert [tool.name for tool in listed] == ["checked_add", RECORDED_TOOL_NAME]
    assert listed[1].input_schema == thaw(checked_add_recorded_spec().input_schema)
    assert listed[1].output_schema == thaw(checked_add_recorded_spec().output_schema)
    assert not first.is_error and first.structured_content["result"] == 5
    assert first.structured_content["replayed"] is False
    assert again.structured_content == {**first.structured_content, "replayed": True}
