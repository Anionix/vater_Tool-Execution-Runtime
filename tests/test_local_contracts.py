"""Contract acceptance through the two existing standard-I/O entry points."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
from time import monotonic_ns
from uuid import UUID

import pytest

from verified_execution.core import LOCAL_PUBLISHER, LocalAuthority
from verified_execution.ledger import LedgerError, OperationLedger, ResourceSnapshot
from verified_execution.worker_leases import LeasedLedger

ROOT = Path(__file__).resolve().parents[1]
CAPABILITIES = json.loads((ROOT / "contracts/local_capabilities.json").read_text())


@contextmanager
def running_server(tmp_path, *, recorded=False, invalid_frames=(), measure=None, children=None):
    pytest.importorskip("mcp")
    log = tmp_path / "diagnostics"
    command = [sys.executable, "-I", str(ROOT / "scripts/run_local_mcp.py")]
    environment = dict(os.environ)
    if recorded:
        command = [sys.executable, str(ROOT / "server_mcp.py")]
        environment.update(PYTHONPATH=str(ROOT / "src"),
                           VERIFIED_EXECUTION_LEDGER_PATH=str(tmp_path / "history.sqlite3"))
    with log.open("wb") as errors, subprocess.Popen(
        command, cwd=ROOT, env=environment, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=errors,
    ) as child:
        def exchange(identifier, method, params):
            params = {**params, "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                         "io.modelcontextprotocol/clientCapabilities": {}}}
            message = {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
            frame = (json.dumps(message) + "\n").encode()
            started = monotonic_ns()
            child.stdin.write(frame)
            child.stdin.flush()
            assert select.select([child.stdout], [], [], 5)[0], "missing protocol response"
            response = json.loads(child.stdout.readline())
            if measure is not None:
                measure("input_bytes", len(frame))
                measure("response_elapsed_ns", monotonic_ns() - started)
            if children is not None:
                observed = subprocess.check_output(
                    ["ps", "-o", "pid=", "--ppid", str(child.pid)], text=True, timeout=2
                )
                children.update(observed.split())
            assert response["id"] == identifier
            return response
        try:
            for frame in invalid_frames:
                child.stdin.write(frame)
                child.stdin.flush()
                assert select.select([child.stdout], [], [], 5)[0], "missing framing error"
                response = json.loads(child.stdout.readline())
                assert response["error"]["code"] == -32700 and response["id"] is None
            yield exchange
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                raise
            assert child.returncode == 0


def test_protocol_routes_do_not_execute_query_or_model_requests(tmp_path):
    from_method = {"name": "checked_add", "arguments": {"left": 3, "right": 5}}
    with running_server(tmp_path) as exchange:
        catalog = exchange("list", "tools/list", from_method)["result"]
        assert [tool["name"] for tool in catalog["tools"]] == ["checked_add"]
        for method, valid_params in (("sampling/createMessage", {"messages": [], "maxTokens": 1}),
                                     ("resources/read", {"uri": "example:record"}),
                                     ("tasks/cancel", {"taskId": "unknown"})):
            response = exchange(method, method, {**from_method, **valid_params})
            assert "error" in response and "result" not in response, method
        reply = exchange("calculation", "tools/call", from_method)["result"]
        assert reply["structuredContent"] == {"result": 8}
    events = [json.loads(line) for line in (tmp_path / "diagnostics").read_text().splitlines()]
    assert [event["outcome"] for event in events] == ["succeeded"]


def test_protocol_capabilities_and_trusted_publisher(tmp_path):
    for recorded, entry in [(False, "minimum_stdio"), (True, "recorded_stdio")]:
        profile = CAPABILITIES["entrypoints"][entry]
        with running_server(tmp_path, recorded=recorded) as exchange:
            capabilities = exchange(1, "server/discover", {})["result"]["capabilities"]
            listed = exchange(2, "tools/list", {})["result"]["tools"]
            assert profile["tools/list"] is True and "tools" in capabilities
            assert [item["name"] for item in listed] == profile["tools/call"]
            assert CAPABILITIES["model"] is None and profile["sampling/createMessage"] is False
            metadata = listed[0]["_meta"]["io.github.anionix/contract"]
            assert metadata["publisher"] == CAPABILITIES["publisher"] == LOCAL_PUBLISHER
            authority = LocalAuthority.calculator_only()
            assert authority.permits("checked_add", publisher=LOCAL_PUBLISHER)
            assert not authority.permits("checked_add", publisher="another.publisher")


def test_trace_correlates_retries_without_recording_private_input(tmp_path):
    marker = "secret-synthetic-credential"
    name = "checked_add_recorded"
    with running_server(tmp_path, recorded=True) as exchange:
        operations = []
        for number, key in enumerate(("first", "first", "second"), start=1):
            params = {"name": name, "arguments": {"request_key": key, "left": 2, "right": 1}}
            reply = exchange(marker + str(number), "tools/call", params)["result"]
            operations.append(reply["structuredContent"]["operation_id"])
            assert reply["_meta"]["io.github.anionix/source"] == "local_calculator"
            assert reply["structuredContent"]["replayed"] is (number == 2)
        bad = {"name": name, "arguments": {"request_key": "invalid", "left": marker, "right": 1}}
        assert exchange(4, "tools/call", bad)["result"]["isError"]
    assert operations[0] == operations[1] != operations[2]
    raw = (tmp_path / "diagnostics").read_text()
    assert marker not in raw and "request_key" not in raw
    events = list(map(json.loads, raw.splitlines()))
    assert [e["outcome"] for e in events] == ["succeeded", "succeeded", "succeeded", "rejected"]
    assert [e["operation_id"] for e in events[:3]] == operations
    assert len({UUID(e["attempt_id"]) for e in events}) == 4
    assert len({UUID(e["trace_id"]) for e in events}) == 1
    assert all(e["source"] == "mcp.tools.call" and e["elapsed_ns"] >= 0 for e in events)
    assert events[-1]["error_category"] == "input"
    assert all(marker.encode() not in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())


def test_official_message_types_preserve_provider_metadata():
    pytest.importorskip("mcp")
    from mcp.types import (CallToolRequest, CancelTaskRequest, CreateMessageRequest,
                           CreateMessageResult, ReadResourceRequest)
    assert [t.model_fields["method"].default for t in (
        CallToolRequest, CreateMessageRequest, ReadResourceRequest, CancelTaskRequest
    )] == ["tools/call", "sampling/createMessage", "resources/read", "tasks/cancel"]
    # A provider can carry nested extension values through the supported SDK conversion.
    payload = {"model": "sample-model", "role": "assistant", "content": {"type": "text", "text": "ok"},
               "_meta": {"example.org/provider": {"options": [False, None, {"revision": "1"}]}}}
    result = CreateMessageResult.model_validate(payload)
    assert result.model_dump(mode="json", by_alias=True, exclude_unset=True) == payload


@pytest.mark.parametrize("leased", [False, True], ids=["approval", "lease"])
def test_dispatch_compares_current_resource_version(tmp_path, leased):
    from time import time_ns

    versions = {"resource": ResourceSnapshot("owner", "one")}
    database = LeasedLedger if leased else OperationLedger
    ledger = database(tmp_path / "history.sqlite3", read_resource=versions.get)
    args = dict(principal="owner", tool_name="checked_add", target_id="resource",
                resource_version="one", arguments={"left": 1, "right": 2})
    op = ledger.reserve(**args, requires_approval=not leased, request_key="one")
    if leased:
        token = ledger.new_session().token
        def dispatch():
            return ledger.start(op.operation_id, token=token)
    else:
        ledger.issue_approval(**args, approval_id="one", expires_at_ms=time_ns() // 1_000_000 + 60_000)
        def dispatch():
            return ledger.begin_dispatch(op.operation_id, approval_id="one")
    versions["resource"] = ResourceSnapshot("owner", "two")
    with pytest.raises(LedgerError, match="RESOURCE_VERSION_CHANGED"):
        dispatch()
    assert ledger.history(op.operation_id) == ["reserved"]
    versions["resource"] = ResourceSnapshot("another owner", "one")
    with pytest.raises(LedgerError, match="NOT_AUTHORIZED"):
        dispatch()
    assert ledger.history(op.operation_id) == ["reserved"]
    versions["resource"] = ResourceSnapshot("owner", "one")
    assert dispatch().state == "dispatched"


def test_version_one_contract_remains_compatible(tmp_path):
    contract = json.loads((ROOT / "contracts/checked_add.output.contract.json").read_text())
    # Check the archived v1 examples with test_contracts.test_expected_case on every run.
    assert hashlib.sha256((ROOT / "contracts/expected_cases.jsonl").read_bytes()).hexdigest() == "8185d1a22a038e10517f76887cde19f348ab0dbdd4d6ed20734b9b41c37e7a34"
    assert re.fullmatch(r"1\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", contract["contract_version"])
    with running_server(tmp_path) as exchange:
        tool = exchange(1, "tools/list", {})["result"]["tools"][0]
    assert tool["inputSchema"] == json.loads((ROOT / "contracts/checked_add.input.schema.json").read_text())
    assert tool["_meta"]["io.github.anionix/contract"]["version"] == contract["contract_version"]
    assert tool["outputSchema"] == {"type": "object", "required": ["result"], "additionalProperties": False,
                                   "properties": {"result": {"type": "integer", "minimum": 0, "maximum": 4294967295}}}


def test_published_error_categories_are_separate():
    codes = CAPABILITIES["error_categories"]
    assert set(codes) == {"input", "authentication", "authorization", "contract", "external", "unknown"}
    distinct = {code for group in codes.values() for code in group}
    assert len(distinct) == sum(map(len, codes.values()))
    assert [group[0] for group in (codes["input"], codes["authentication"], codes["authorization"],
                                    codes["contract"], codes["external"], codes["unknown"])] == [
        "INVALID_ARGUMENT", "INVALID_PRINCIPAL", "NOT_AUTHORIZED", "UNKNOWN_TOOL", "TIMEOUT", "OUTCOME_UNKNOWN"
    ]


def test_developer_inputs_share_the_preparse_depth_limit(monkeypatch):
    from verified_execution.local_cli import InputFormatError, decode_line
    from verified_execution.recorded_cli import decode_recorded_request
    data = b'{"name":"checked_add","arguments":' + b'[' * 32 + b'0' + b']' * 32 + b'}'
    def unexpected_parse(*args, **kwargs):
        pytest.fail("over-limit data reached json.loads")
    monkeypatch.setattr(json, "loads", unexpected_parse)
    for decode in [decode_line, decode_recorded_request]:
        with pytest.raises(InputFormatError):
            decode(data)


@pytest.mark.skipif(not Path("/proc/self/task").exists(), reason="Linux process observation")
def test_fixed_server_recovers_measures_input_and_reaps_child(tmp_path, record_property):
    limits = json.loads((ROOT / "config/limits.json").read_text())
    over_size = b'{"padding":"' + b'x' * limits["request_input_max_bytes"] + b'"}\n'
    over_depth = b'{"v":' + b'[' * limits["json_max_depth"] + b'0' + b']' * limits["json_max_depth"] + b'}\n'
    children = set()
    with running_server(tmp_path, invalid_frames=[b'{bad}\n', b'\xff\n', over_size, over_depth],
                        measure=record_property, children=children) as exchange:
        reply = exchange("after", "tools/call", {"name": "checked_add", "arguments": {"left": 7, "right": 9}})
        assert reply["result"]["structuredContent"] == {"result": 16}
    assert children and all(not Path("/proc", pid).exists() for pid in children)
