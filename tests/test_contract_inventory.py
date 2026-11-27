"""task.testing.002: the contract test inventory.

Every tool contract in contracts/ is listed in INVENTORY; a contract without
an entry fails test_every_contract_is_in_the_inventory. For each listed tool:

* Request side: violations are derived from the input schema itself (a
  missing required field, an extra field, each property with each wrong JSON
  type, each bound crossed by one). jsonschema confirms each one really
  violates the schema; then the tool's validation, the shared runtime, MCP and
  HTTP must refuse it without running anything.
* Response side: replies are derived from the output contract's success
  domain with the wrong type or out of range, and are refused as
  INVALID_TOOL_RESULT. Failure codes outside the contract's failure domain
  never reach a client (CONTRACT-FAILURE-001), and every failure observed over
  a corpus of real failure scenarios is declared by the contract.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path

import jsonschema
import pytest

from adapter_drivers import TOKEN, asgi, load_mcp_adapter, with_sdk_client
from test_external_change import FakeGitHub, Host
from verified_execution.arithmetic import checked_multiply_spec
from verified_execution.cancellable_bridge import CancellableLeasedToolBridge
from verified_execution.core import LocalAuthority, OperationError
from verified_execution.durable import checked_add_recorded_spec
from verified_execution.external_change import TOOL_NAME as ISSUE_TOOL, github_test_create_issue_spec
from verified_execution.http_local import create_app
from verified_execution.ledger import LedgerError, OperationLedger
from verified_execution.recorded_tool_bridge import (
    RECORDED_TOOL_NAME, BridgeResult, RecordedToolBridge, declared_failures)
from verified_execution.runtime import ToolRuntime
from verified_execution.tools import ToolRegistry, default_registry
from verified_execution.worker_leases import LeasedLedger

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
INVENTORY = {
    "checked_add": (lambda: default_registry().get("checked_add"), {"left": 1, "right": 2}),
    "checked_multiply": (checked_multiply_spec, {"left": 2, "right": 3}),
    RECORDED_TOOL_NAME: (checked_add_recorded_spec, {"request_key": "k", "left": 1, "right": 2}),
    ISSUE_TOOL: (github_test_create_issue_spec, {"request_key": "k", "title": "Contract test"}),
}
READ_ONLY = [name for name in INVENTORY if INVENTORY[name][0]().risk == "read_only"]
DURABLE = [name for name in INVENTORY if name not in READ_ONLY]
GRANT_ALL = LocalAuthority(frozenset({"checked_add", "checked_multiply", ISSUE_TOOL}))
ADAPTER_CODES = {code for codes in json.loads((CONTRACTS / "local_capabilities.json").read_text())[
    "error_categories"].values() for code in codes}


def schema(name):
    return json.loads((CONTRACTS / f"{name}.input.schema.json").read_text(encoding="utf-8"))


def output_contract(name):
    return json.loads((CONTRACTS / f"{name}.output.contract.json").read_text(encoding="utf-8"))


def declared_codes(name):
    return set(output_contract(name)["domains"]["failure"]["error_code"]["enum"])


def request_violations(name):
    """Schema-derived violations: (label, payload). Nothing here reads the implementation."""
    spec, valid = schema(name), INVENTORY[name][1]
    cases = [(f"top-level {type(value).__name__}", value) for value in (None, [], "text", 1, True)]
    cases += [(f"missing {field}", {k: v for k, v in valid.items() if k != field}) for field in spec["required"]]
    if spec.get("additionalProperties") is False:
        cases.append(("extra field", {**valid, "unexpected": 1}))
    wrong = {"integer": ["1", 1.5, True, None, []], "string": [1, None, True, []]}
    for field, rule in spec["properties"].items():
        cases += [(f"{field} as {type(value).__name__}", {**valid, field: value}) for value in wrong[rule["type"]]]
        if "minimum" in rule:
            cases.append((f"{field} below minimum", {**valid, field: rule["minimum"] - 1}))
        if "maximum" in rule:
            cases.append((f"{field} above maximum", {**valid, field: rule["maximum"] + 1}))
        if rule.get("minLength", 0) >= 1:
            cases.append((f"{field} too short", {**valid, field: ""}))
        if "maxLength" in rule:
            cases.append((f"{field} too long", {**valid, field: "x" * (rule["maxLength"] + 1)}))
    return cases


REQUEST_CASES = [(name, label, payload) for name in INVENTORY for label, payload in request_violations(name)]


def case_id(case):
    return f"{case[0]}-{case[1]}"


class Spy:
    """A run function or durable executor that records whether it was reached."""

    def __init__(self, reply=None):
        self.calls, self.reply = [], reply

    def run(self, trusted):
        self.calls.append(trusted)
        return self.reply

    def call(self, name, arguments):
        self.calls.append(arguments)
        return self.reply

    async def acall(self, name, arguments):
        return self.call(name, arguments)


def runtime_with_spy(name, reply=None):
    spy = Spy(reply)
    spec = INVENTORY[name][0]()
    if name in READ_ONLY:
        return ToolRuntime(GRANT_ALL, ToolRegistry([replace(spec, run=spy.run)])), spy
    return ToolRuntime(GRANT_ALL, ToolRegistry([spec]), durable={name: spy}), spy


# --- the inventory itself --------------------------------------------------------

def test_every_contract_is_in_the_inventory():
    names = {path.name.removesuffix(".input.schema.json") for path in CONTRACTS.glob("*.input.schema.json")}
    assert names == set(INVENTORY)
    for name in names:
        assert (CONTRACTS / f"{name}.output.contract.json").is_file()
        assert INVENTORY[name][0]().name == name


@pytest.mark.parametrize("name", INVENTORY)
def test_valid_sample_is_accepted_so_violations_are_meaningful(name):
    jsonschema.validate(INVENTORY[name][1], schema(name))
    assert INVENTORY[name][0]().validate(INVENTORY[name][1]) is not None


# --- request side ---------------------------------------------------------------------

@pytest.mark.parametrize("case", REQUEST_CASES, ids=case_id)
def test_request_violation_is_refused_before_anything_runs(case):
    name, _label, payload = case
    assert not jsonschema.Draft202012Validator(schema(name)).is_valid(payload)  # a real violation
    with pytest.raises(OperationError):
        INVENTORY[name][0]().validate(payload)
    runtime, spy = runtime_with_spy(name, reply=BridgeResult(True, 1, "op", False, None))
    outcome = asyncio.run(runtime.acall(name, payload))
    assert not outcome.ok and outcome.error_code in declared_codes(name) and spy.calls == []


@pytest.mark.parametrize("case", [c for c in REQUEST_CASES if c[0] in READ_ONLY], ids=case_id)
def test_request_violation_is_refused_by_mcp_and_http(case):
    pytest.importorskip("mcp")
    name, _label, payload = case
    registry = ToolRegistry([INVENTORY[name][0]()])
    server = load_mcp_adapter("server_mcp_contract_inventory").build_server(GRANT_ALL, registry=registry)

    async def scenario(client):
        return await client.call_tool(name, payload)

    if payload is None or isinstance(payload, dict):  # MCP arguments are optional
        reply = with_sdk_client(server, scenario)
        assert reply.is_error and reply.content[0].text in declared_codes(name)
    else:
        # MCP arguments are a JSON object by protocol: the SDK's request model, which
        # the server also parses every request with, refuses anything else.
        from mcp.types import CallToolRequestParams
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            CallToolRequestParams.model_validate({"name": name, "arguments": payload})
    status, body = asyncio.run(asgi(create_app(TOKEN, GRANT_ALL, registry=registry), "POST",
                                    f"/tools/{name}/jobs", json.dumps(payload).encode()))
    assert (status, body) == (400, {"error": "INVALID_BODY"})


# --- response side: success domain ------------------------------------------------------

def success_violations(name):
    domain = output_contract(name)["domains"]["success"]
    rule = domain.get("result") or domain["properties"]["result"]
    bad = ["3", 3.5, True, None, [3]]
    if "minimum" in rule:
        bad.append(rule["minimum"] - 1)
    if "maximum" in rule:
        bad.append(rule["maximum"] + 1)
    return bad


RESPONSE_CASES = [(name, value) for name in INVENTORY for value in success_violations(name)]


@pytest.mark.parametrize("case", RESPONSE_CASES, ids=lambda case: f"{case[0]}-{case[1]!r}")
def test_reply_outside_the_success_domain_is_refused(case):
    name, value = case
    reply = value if name in READ_ONLY else BridgeResult(True, value, "op-1", False, None)
    runtime, spy = runtime_with_spy(name, reply=reply)
    outcome = asyncio.run(runtime.acall(name, INVENTORY[name][1]))
    assert spy.calls and (outcome.ok, outcome.value, outcome.error_code) == (False, None, "INVALID_TOOL_RESULT")


@pytest.mark.parametrize("name", DURABLE)
@pytest.mark.parametrize("bad", [
    BridgeResult(True, 3, None, False, None), BridgeResult(True, 3, "", False, None),
    BridgeResult(True, 3, "op-1", None, None), BridgeResult(True, 3, 7, False, None),
], ids=["no-operation-id", "empty-operation-id", "replayed-not-bool", "operation-id-not-string"])
def test_durable_reply_missing_required_success_fields_is_refused(name, bad):
    runtime, _spy = runtime_with_spy(name, reply=bad)
    outcome = asyncio.run(runtime.acall(name, INVENTORY[name][1]))
    assert (outcome.ok, outcome.error_code) == (False, "INVALID_TOOL_RESULT")


def test_reply_in_range_but_not_the_answer_is_refused():
    runtime, _spy = runtime_with_spy("checked_add", reply=4)  # 1 + 2 is not 4
    assert asyncio.run(runtime.acall("checked_add", {"left": 1, "right": 2})).error_code == "INVALID_TOOL_RESULT"


# --- response side: failure domain ------------------------------------------------------

@pytest.mark.parametrize("name", DURABLE)
def test_executor_failure_codes_come_from_the_contract(name):
    assert declared_failures(name) == frozenset(declared_codes(name))


def raising(code):
    def fail(*_args, **_kwargs):
        raise LedgerError(code)
    return fail


def recorded_legacy(tmp_path, monkeypatch, code):
    ledger = OperationLedger(tmp_path / "legacy.sqlite3")
    monkeypatch.setattr(ledger, "begin_dispatch", raising(code))
    bridge = RecordedToolBridge(ledger, principal="host", authority=GRANT_ALL)
    return lambda: bridge.call(RECORDED_TOOL_NAME, INVENTORY[RECORDED_TOOL_NAME][1])


def recorded_leased(tmp_path, monkeypatch, code):
    ledger = LeasedLedger(tmp_path / "leased.sqlite3")
    monkeypatch.setattr(ledger, "start", raising(code))
    bridge = CancellableLeasedToolBridge(ledger, principal="host", authority=GRANT_ALL)
    return lambda: asyncio.run(bridge.acall(RECORDED_TOOL_NAME, INVENTORY[RECORDED_TOOL_NAME][1]))


def external(tmp_path, monkeypatch, code):
    host = Host(tmp_path / "external.sqlite3", FakeGitHub())
    monkeypatch.setattr(host.ledger, "begin_dispatch", raising(code))
    return lambda: host.executor.call(ISSUE_TOOL, INVENTORY[ISSUE_TOOL][1])


@pytest.mark.parametrize("make,tool", [(recorded_legacy, RECORDED_TOOL_NAME), (recorded_leased, RECORDED_TOOL_NAME),
                                       (external, ISSUE_TOOL)], ids=["legacy", "leased", "external"])
@pytest.mark.parametrize("code", ["RESOURCE_VERSION_CHANGED", "SESSION_EXPIRED", "NOT_CALCULATION_DISPATCHED"])
def test_undeclared_internal_code_never_reaches_a_client(tmp_path, monkeypatch, make, tool, code):
    assert code not in declared_codes(tool)
    result = make(tmp_path, monkeypatch, code)()
    assert (result.ok, result.error_code) == (False, "INVALID_TOOL_RESULT")


def test_failure_corpus_stays_within_the_contracts(tmp_path):
    """Real failure scenarios for every tool; each observed code must be declared."""
    observed: dict[str, set[str]] = {name: set() for name in INVENTORY}

    def note(name, outcome):
        assert not outcome.ok, (name, outcome)
        observed[name].add(outcome.error_code)

    def run(runtime, name, arguments):
        return asyncio.run(runtime.acall(name, arguments))

    registry = default_registry().with_tools(checked_multiply_spec(), checked_add_recorded_spec(),
                                             github_test_create_issue_spec())
    github = FakeGitHub()
    host = Host(tmp_path / "external.sqlite3", github)
    recorded = RecordedToolBridge(OperationLedger(tmp_path / "recorded.sqlite3"), principal="host",
                                  authority=GRANT_ALL)
    durable = {ISSUE_TOOL: host.executor, RECORDED_TOOL_NAME: recorded}
    runtime = ToolRuntime(GRANT_ALL, registry, durable=durable)
    denied = ToolRuntime(LocalAuthority(frozenset()), registry, durable=durable)
    for name in INVENTORY:
        for _label, payload in request_violations(name):
            note(name, run(runtime, name, payload))
        note(name, run(denied, name, INVENTORY[name][1]))
    for name in ("checked_add", "checked_multiply"):
        note(name, run(runtime, name, {"left": 4294967295, "right": 2}))
    assert run(runtime, RECORDED_TOOL_NAME, {"request_key": "r", "left": 1, "right": 2}).ok
    note(RECORDED_TOOL_NAME, run(runtime, RECORDED_TOOL_NAME, {"request_key": "r", "left": 1, "right": 3}))
    note(RECORDED_TOOL_NAME, run(runtime, RECORDED_TOOL_NAME, {"request_key": "o", "left": 4294967295, "right": 1}))
    note(RECORDED_TOOL_NAME, run(runtime, RECORDED_TOOL_NAME, {"request_key": "o", "left": 4294967295, "right": 1}))

    def issue(key, title="Contract test"):
        return run(runtime, ISSUE_TOOL, {"request_key": key, "title": title})

    assert issue("i").ok
    note(ISSUE_TOOL, issue("i", "Another title"))
    for script in (["rate_limited", "rate_limited"], ["reject"], ["drop_reply"], ["wrong_title"]):
        github.script = list(script)
        key = "-".join(script)
        github.list_lag = script == ["drop_reply"]
        note(ISSUE_TOOL, issue(key))
        replay = issue(key)
        if script == ["wrong_title"]:
            # The reply named another title, but the real issue matches: the replay
            # reconciles it by lookup. That is a success, not a contract failure.
            assert replay.ok and replay.replayed
        else:
            note(ISSUE_TOOL, replay)
    github.list_lag = False
    no_approval = Host(tmp_path / "external-2.sqlite3", FakeGitHub())
    no_approval.executor.approve = None
    note(ISSUE_TOOL, asyncio.run(ToolRuntime(GRANT_ALL, registry, durable={ISSUE_TOOL: no_approval.executor})
                                 .acall(ISSUE_TOOL, {"request_key": "n", "title": "Contract test"})))
    for name, codes in observed.items():
        assert codes and codes <= declared_codes(name), (name, codes - declared_codes(name))
    # The corpus reached the durable tools' distinctive outcomes, not only input errors.
    assert {"IDEMPOTENCY_CONFLICT", "ALREADY_RECORDED"} <= observed[RECORDED_TOOL_NAME]
    assert {"IDEMPOTENCY_CONFLICT", "RATE_LIMITED", "REJECTED_BY_DESTINATION", "HUMAN_RECONCILIATION_REQUIRED",
            "TARGET_MISMATCH", "APPROVAL_REQUIRED"} <= observed[ISSUE_TOOL]


def test_mcp_adapter_codes_are_declared_by_the_adapter_contract():
    """A server-level refusal (here the host's call budget) is an adapter code,
    declared in contracts/local_capabilities.json rather than by each tool."""
    pytest.importorskip("mcp")
    server = load_mcp_adapter("server_mcp_contract_budget").build_server(call_budget=0)

    async def scenario(client):
        return await client.call_tool("checked_add", {"left": 1, "right": 2})

    reply = with_sdk_client(server, scenario)
    assert reply.is_error and reply.content[0].text in ADAPTER_CODES | declared_codes("checked_add")
