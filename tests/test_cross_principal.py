"""task.testing.005: one principal referencing another principal's target.

Alice and Bob share one ledger file, as two host processes on one machine
would. Bob reuses Alice's request key, names her operation id, presents her
approval, or calls the HTTP API with his own credential. Every path must
return none of Alice's data: no value, no issue number, no stored result, no
replay of her operation.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from adapter_drivers import TOKEN, asgi, load_mcp_adapter, with_sdk_client
from test_external_change import FakeGitHub, Host
from verified_execution.core import LocalAuthority
from verified_execution.http_local import create_app
from verified_execution.ledger import LedgerError, OperationLedger
from verified_execution.recorded_tool_bridge import RECORDED_TOOL_NAME, RecordedToolBridge
from verified_execution.worker_leases import LeasedLedger

ADD = LocalAuthority(frozenset({"checked_add"}))
BOB_TOKEN = "bobsOwnCredential" * 3


def recorded(tool, key, left, right):
    return tool.call(RECORDED_TOOL_NAME, {"request_key": key, "left": left, "right": right})


# --- ledger -------------------------------------------------------------------

@pytest.mark.parametrize("ledger_type", [OperationLedger, LeasedLedger], ids=["legacy", "leased"])
def test_saved_result_is_readable_only_by_its_principal(tmp_path, ledger_type):
    ledger = ledger_type(tmp_path / "shared.sqlite3")
    alice = RecordedToolBridge(OperationLedger(tmp_path / "shared.sqlite3"), principal="alice", authority=ADD)
    first = recorded(alice, "shared-key", 20, 22)
    assert first.ok and first.value == 42
    assert ledger.read_calculation(first.operation_id, principal="alice") == 42
    assert ledger.read_calculation(first.operation_id, principal="bob") is None


def test_same_request_key_is_a_different_operation_for_another_principal(tmp_path):
    path = tmp_path / "shared.sqlite3"
    alice = RecordedToolBridge(OperationLedger(path), principal="alice", authority=ADD)
    bob = RecordedToolBridge(OperationLedger(path), principal="bob", authority=ADD)
    mine = recorded(alice, "shared-key", 20, 22)
    # Different arguments: Bob gets his own value, not Alice's and not a conflict
    # that would reveal that her key exists.
    other = recorded(bob, "shared-key", 1, 2)
    # Same arguments as hers: still Bob's own fresh operation, never a replay.
    same = recorded(bob, "shared-key-2", 20, 22)
    again = recorded(bob, "shared-key", 1, 2)
    assert (other.ok, other.value, other.replayed) == (True, 3, False)
    assert (same.ok, same.value, same.replayed) == (True, 42, False)
    assert other.operation_id != mine.operation_id and same.operation_id != mine.operation_id
    assert again.replayed and again.operation_id == other.operation_id and again.value == 3


def test_approval_for_one_principal_cannot_dispatch_anothers_operation(tmp_path):
    ledger = OperationLedger(tmp_path / "shared.sqlite3")
    arguments = {"title": "change"}
    ledger.issue_approval(approval_id="alice-approval", principal="alice", tool_name="write", target_id="t",
                          resource_version="v1", arguments=arguments,
                          expires_at_ms=int(time.time() * 1000) + 60_000)
    bob_op = ledger.reserve(principal="bob", request_key="k", tool_name="write", target_id="t",
                            resource_version="v1", arguments=arguments, requires_approval=True)
    with pytest.raises(LedgerError) as refused:
        ledger.begin_dispatch(bob_op.operation_id, approval_id="alice-approval")
    assert refused.value.code == "APPROVAL_INVALID"
    assert ledger.get(bob_op.operation_id).state == "prepared"


# --- shared runtime through MCP ---------------------------------------------------

@pytest.mark.parametrize("leased", [False, True], ids=["legacy", "leased"])
def test_mcp_never_replays_another_principals_recorded_call(tmp_path, leased):
    pytest.importorskip("mcp")
    adapter = load_mcp_adapter(f"server_mcp_cross_principal_{leased}")
    path = str(tmp_path / "shared.sqlite3")
    servers = {name: adapter.build_server(ADD, ledger_path=path, principal=name, leased=leased)
               for name in ("alice", "bob")}

    def call(name, key, left, right):
        async def scenario(client):
            return await client.call_tool(RECORDED_TOOL_NAME, {"request_key": key, "left": left, "right": right})
        return with_sdk_client(servers[name], scenario)

    hers = call("alice", "shared-key", 20, 22)
    his = call("bob", "shared-key", 1, 2)
    assert hers.structured_content["result"] == 42
    assert his.structured_content["result"] == 3 and his.structured_content["replayed"] is False
    assert his.structured_content["operation_id"] != hers.structured_content["operation_id"]
    assert "42" not in json.dumps([block.model_dump(mode="json") for block in his.content])


# --- external results ------------------------------------------------------------------

def test_external_result_is_readable_only_by_its_principal(tmp_path):
    github = FakeGitHub()
    alice = Host(tmp_path / "shared.sqlite3", github)
    bob = Host(tmp_path / "shared.sqlite3", github)
    bob.executor.principal, bob.approvals = "bob", 1000  # distinct approval ids in the shared ledger
    hers = alice.create(key="shared-key")
    assert hers.ok and alice.executor.result(hers.operation_id)["object_id"] == str(hers.value)
    assert bob.executor.result(hers.operation_id) is None
    assert bob.executor.result("no-such-operation") is None
    # Bob's call with Alice's key is his own operation, with his own approval; it is
    # never answered with her issue number.
    his = bob.create(key="shared-key")
    assert his.operation_id != hers.operation_id and not his.replayed
    assert his.value != hers.value and len(github.issues) == 2


# --- HTTP ----------------------------------------------------------------------------

async def request_with(app, method, path, token):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
               "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
               "root_path": "", "server": ("127.0.0.1", 80), "client": ("127.0.0.1", 1),
               "headers": [(b"authorization", f"Bearer {token}".encode())]}, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, json.loads(b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body"))


def test_http_jobs_are_not_visible_to_another_credential():
    async def scenario():
        alice_app, bob_app = create_app(TOKEN), create_app(BOB_TOKEN)
        status, started = await asgi(alice_app, "POST", "/jobs", b'{"left": 20, "right": 22}')
        assert status == 202
        job = f"/jobs/{started['job_id']}"
        for _ in range(200):
            _, seen = await asgi(alice_app, "GET", job)
            if seen["state"] == "succeeded":
                break
            await asyncio.sleep(0.01)
        assert seen["result"] == 42
        return (await request_with(bob_app, "GET", job, BOB_TOKEN),
                await request_with(bob_app, "DELETE", job, BOB_TOKEN),
                await request_with(alice_app, "GET", job, BOB_TOKEN))

    missing, cancelled, refused = asyncio.run(scenario())
    # Bob's server does not know Alice's job, and her server refuses his credential.
    assert missing == (404, {"error": "UNKNOWN_JOB"}) and cancelled == (404, {"error": "UNKNOWN_JOB"})
    assert refused == (401, {"error": "UNAUTHORIZED"})
