"""Shared ToolSpec / ToolRegistry / ToolRuntime used by every protocol adapter."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path

import pytest

from adapter_drivers import TOKEN, asgi, load_mcp_adapter, with_sdk_client
from verified_execution.core import CalculationService, LocalAuthority, OperationError, UNSIGNED_32_MAX
from verified_execution.runtime import ToolRuntime
from verified_execution.tools import ToolRegistry, ToolSpec, default_registry, load_registry, thaw

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"


def _validate_value(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {"value"}:
        raise OperationError("INVALID_ARGUMENT", "arguments require only value")
    value = arguments["value"]
    if type(value) is not int or not 0 <= value <= UNSIGNED_32_MAX:
        raise OperationError("INVALID_ARGUMENT", "value must be an unsigned 32-bit integer")
    return value


def _verify_double(value, result):
    if type(result) is not int or result != value * 2:
        raise OperationError("INVALID_TOOL_RESULT", "invalid result")


def double_spec(run=lambda value: value * 2, risk="read_only", name="double"):
    """A second tool defined only here; no adapter code mentions it."""
    return ToolSpec(
        name=name, description="Double an unsigned integer.", risk=risk,
        input_schema={"type": "object", "properties": {"value": {"type": "integer", "minimum": 0}},
                      "required": ["value"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {"result": {"type": "integer"}},
                       "required": ["result"], "additionalProperties": False},
        validate=_validate_value, run=run, verify_result=_verify_double, contract_version="0.1.0",
    )


def both_tools():
    return default_registry().with_tools(double_spec())


def authority(*names):
    return LocalAuthority(frozenset(names))


# --- registry -------------------------------------------------------------

def test_registry_schemas_come_from_contract_files():
    spec = load_registry(CONTRACTS).get("checked_add")
    contract = json.loads((CONTRACTS / "checked_add.input.schema.json").read_text())
    output = json.loads((CONTRACTS / "checked_add.output.contract.json").read_text())
    risks = json.loads((CONTRACTS / "tool_risks.json").read_text())["tools"]
    assert thaw(spec.input_schema) == contract
    assert thaw(spec.output_schema)["properties"]["result"] == output["domains"]["success"]["result"]
    assert spec.contract_version == output["contract_version"]
    assert spec.risk == risks["checked_add"]


def test_registry_rejects_duplicates_and_unknown_risk():
    with pytest.raises(ValueError, match="duplicate"):
        ToolRegistry([double_spec(), double_spec()])
    with pytest.raises(ValueError, match="risk"):
        double_spec(risk="mostly_harmless")


def test_specs_are_read_only_for_adapters():
    spec = default_registry().get("checked_add")
    with pytest.raises(TypeError):
        spec.input_schema["properties"]["left"]["maximum"] = 2**64
    copy = thaw(spec.input_schema)
    copy["properties"]["left"]["maximum"] = 2**64
    assert spec.input_schema["properties"]["left"]["maximum"] == UNSIGNED_32_MAX


def test_with_tools_leaves_original_registry_unchanged():
    base = default_registry()
    extended = base.with_tools(double_spec())
    assert base.names() == {"checked_add"}
    assert extended.names() == {"checked_add", "double"}


# --- runtime --------------------------------------------------------------

def test_calculation_service_is_the_shared_runtime():
    assert CalculationService is ToolRuntime


def test_runtime_executes_any_registered_read_only_tool():
    runtime = ToolRuntime(authority("checked_add", "double"), both_tools())
    assert runtime.execute("double", {"value": 21}).result == 42
    assert runtime.execute("checked_add", {"left": 40, "right": 2}).result == 42
    assert runtime.execute("double", {"value": 21}).states == (
        "received", "validated", "authorized", "running", "completed")


def test_runtime_uses_host_authority_per_tool():
    runtime = ToolRuntime(authority("checked_add"), both_tools())
    denied = runtime.execute("double", {"value": 1})
    assert denied.error_code == "NOT_AUTHORIZED"
    assert denied.states == ("received", "validated", "rejected")


def test_runtime_refuses_non_read_only_tools():
    registry = default_registry().with_tools(double_spec(risk="state_change", name="mutate"))
    runtime = ToolRuntime(authority("checked_add", "mutate"), registry)
    assert [spec.name for spec in runtime.tools()] == ["checked_add"]
    assert runtime.execute("mutate", {"value": 1}).error_code == "UNSUPPORTED_OPERATION"


def test_runtime_verifies_results_before_they_leave():
    wrong = default_registry().with_tools(double_spec(run=lambda value: value * 3))
    outcome = ToolRuntime(authority("double"), wrong).execute("double", {"value": 2})
    assert (outcome.result, outcome.error_code) == (None, "INVALID_TOOL_RESULT")
    assert outcome.states[-1] == "failed"


def test_runtime_hides_tool_bugs_behind_a_fixed_code():
    def broken(_value):
        raise RuntimeError("secret internal detail")
    outcome = ToolRuntime(authority("double"), default_registry().with_tools(double_spec(run=broken))) \
        .execute("double", {"value": 2})
    assert (outcome.result, outcome.error_code) == (None, "EXECUTION_FAILED")


def test_release_rechecks_grant_and_result():
    from threading import Event
    revoked = Event()
    runtime = ToolRuntime(LocalAuthority(frozenset({"checked_add"}), revocation_event=revoked))
    arguments = {"left": 2, "right": 3}
    outcome = runtime.execute("checked_add", arguments)
    assert runtime.release("checked_add", arguments, outcome).result == 5
    forged = replace(outcome, result=6)
    assert runtime.release("checked_add", arguments, forged).error_code == "INVALID_TOOL_RESULT"
    revoked.set()
    assert runtime.release("checked_add", arguments, outcome).error_code == "NOT_AUTHORIZED"


# --- adapters only translate ----------------------------------------------

@pytest.fixture
def mcp_adapter():
    pytest.importorskip("mcp")
    return load_mcp_adapter("server_mcp_runtime_test")




def test_mcp_serves_a_new_tool_without_adapter_changes(mcp_adapter):
    server = mcp_adapter.build_server(authority("checked_add", "double"), registry=both_tools())

    async def scenario(client):
        listed = (await client.list_tools()).tools
        assert [tool.name for tool in listed] == ["checked_add", "double"]
        assert listed[1].input_schema == thaw(double_spec().input_schema)
        doubled = await client.call_tool("double", {"value": 21})
        assert not doubled.is_error and doubled.structured_content == {"result": 42}
        rejected = await client.call_tool("double", {"value": -1})
        assert rejected.is_error and rejected.content[0].text == "INVALID_ARGUMENT"

    with_sdk_client(server, scenario)


def test_mcp_advertises_the_registry_spec(mcp_adapter):
    async def scenario(client):
        return (await client.list_tools()).tools[0].input_schema

    published = with_sdk_client(mcp_adapter.build_server(), scenario)
    assert published == json.loads((CONTRACTS / "checked_add.input.schema.json").read_text())




def test_http_serves_a_new_tool_without_adapter_changes():
    pytest.importorskip("starlette")
    from verified_execution.http_local import create_app

    app = create_app(TOKEN, authority("double"), registry=both_tools(), tool="double")

    async def scenario():
        status, spec = await asgi(app, "GET", "/openapi.json")
        assert status == 200
        schema = spec["paths"]["/jobs"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert schema == thaw(double_spec().input_schema)
        assert (await asgi(app, "POST", "/jobs", b'{"left": 1, "right": 2}'))[0] == 400
        status, started = await asgi(app, "POST", "/jobs", b'{"value": 21}')
        assert status == 202
        for _ in range(200):
            status, job = await asgi(app, "GET", f"/jobs/{started['job_id']}")
            if job["state"] not in {"prepared", "running"}:
                break
            await asyncio.sleep(0.01)
        assert job == {"state": "succeeded", "result": 42, "error": None}

    asyncio.run(scenario())


def test_http_openapi_uses_contract_schema():
    pytest.importorskip("starlette")
    from verified_execution.http_local import create_app

    async def scenario():
        _, spec = await asgi(create_app(TOKEN), "GET", "/openapi.json")
        return spec["paths"]["/jobs"]["post"]["requestBody"]["content"]["application/json"]["schema"]

    assert asyncio.run(scenario()) == json.loads((CONTRACTS / "checked_add.input.schema.json").read_text())


def test_http_refuses_tools_the_runtime_cannot_execute():
    pytest.importorskip("starlette")
    from verified_execution.http_local import create_app

    registry = default_registry().with_tools(double_spec(risk="irreversible", name="drop"))
    for tool in ("drop", "missing"):
        with pytest.raises(ValueError, match="read-only"):
            create_app(TOKEN, registry=registry, tool=tool)
