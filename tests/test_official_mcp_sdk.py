"""REAL SDK INTEGRATION: tests are deliberately skipped if the SDK is absent.

The in-memory test checks SDK dispatch but NOT wire-level communication.
The stdio tests spawn a real Python server through official SDK transports.
"""
import importlib.metadata
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def installed_sdk_v2() -> bool:
    try:
        return importlib.metadata.version("mcp").split(".")[0] == "2"
    except importlib.metadata.PackageNotFoundError:
        return False


pytestmark = pytest.mark.skipif(not installed_sdk_v2(), reason="official MCP Python SDK v2 is unavailable")


async def verify_contract(client):
    from mcp.types import TextContent

    tools = await client.list_tools()
    names = [tool.name for tool in tools.tools]
    assert names == ["checked_add"], names
    published = tools.tools[0]
    assert set(published.input_schema["required"]) == {"left", "right"}
    assert published.input_schema["additionalProperties"] is False
    assert published.output_schema["properties"]["result"]["type"] == "integer"

    cases = [
        ({"left": 100, "right": 200}, 300),
        ({"left": 0, "right": 0}, 0),
        ({"left": 4294967295, "right": 0}, 4294967295),
    ]
    for args, value in cases:
        response = await client.call_tool("checked_add", args)
        assert not response.is_error, (args, response)
        assert response.structured_content == {"result": value}
        text_blocks = [block.text for block in response.content if isinstance(block, TextContent)]
        assert str(value) in text_blocks, (args, text_blocks)

    rejected = [
        {"left": 4294967295, "right": 1},  # overflow
        {"left": -1, "right": 2},          # unsigned range
        {"left": 1.0, "right": 2},         # strict integer token
        {"left": True, "right": 2},        # bool is NOT an integer token
        {"left": "1", "right": 2},         # string
        {"left": 1},                       # required argument absent
        {"left": 1, "right": 2, "other": 3},  # no extra arguments
    ]
    for args in rejected:
        response = await client.call_tool("checked_add", args)
        assert response.is_error, (args, response)

    missing = await client.call_tool("not_registered", {})
    assert missing.is_error


def test_sdk_in_process():
    import anyio
    from mcp import Client
    from server_mcp import server

    async def run():
        async with Client(server, mode="2026-07-28") as client:
            assert client.protocol_version == "2026-07-28"
            await verify_contract(client)

    anyio.run(run)


def test_sdk_stdio_modern():
    import anyio
    from mcp import Client, StdioServerParameters

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-u", str(ROOT / "server_mcp.py")],
            cwd=ROOT,
            env={"PYTHONPATH": str(ROOT / "src")},
        )
        async with Client(params, mode="2026-07-28") as client:
            assert client.protocol_version == "2026-07-28"
            await verify_contract(client)

    anyio.run(run)


def test_sdk_stdio_legacy():
    import anyio
    from mcp import Client, StdioServerParameters

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-u", str(ROOT / "server_mcp.py")],
            cwd=ROOT,
            env={"PYTHONPATH": str(ROOT / "src")},
        )
        async with Client(params, mode="legacy") as client:
            assert client.protocol_version != "2026-07-28"
            await verify_contract(client)

    anyio.run(run)


def test_sdk_access_denied():
    import anyio
    from mcp import Client
    from server_mcp import build_server
    from verified_execution.core import LocalAuthority

    async def run():
        # This is a test-only host policy, NOT a client-provided permission flag.
        denied_server = build_server(LocalAuthority(frozenset()))
        async with Client(denied_server, mode="2026-07-28") as client:
            response = await client.call_tool("checked_add", {"left": 1, "right": 2})
            assert response.is_error
            assert response.structured_content is None

    anyio.run(run)

@pytest.mark.parametrize("era", ["2026-07-28", "legacy"])
def test_sdk_guarded_real_stdio(era, tmp_path):
    """Real client -> fixed release launcher -> guard -> official server."""
    database = tmp_path / "must-not-exist.sqlite"
    import anyio
    from mcp import Client, StdioServerParameters

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-I", "-u", str(ROOT / "scripts" / "run_local_mcp.py")],
            cwd=ROOT,
            env={"VERIFIED_EXECUTION_LEDGER_PATH": str(database),
                 "OPENAI_API_KEY": "synthetic-test-secret"},
        )
        async with Client(params, mode=era) as client:
            if era == "legacy":
                assert client.protocol_version != "2026-07-28"
            else:
                assert client.protocol_version == "2026-07-28"
            await verify_contract(client)

    anyio.run(run)
    assert not database.exists()


def test_sdk_guarded_rejects_raw_duplicate_before_sdk():
    """Raw invalid bytes are rejected BEFORE entering the official MCP parser."""
    import subprocess
    command = [sys.executable, "-u", str(ROOT / "scripts" / "guarded_stdio.py"),
               "--", sys.executable, "-u", str(ROOT / "server_mcp.py")]
    bad = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","method":"tools/call"}\n'
    proc = subprocess.run(command, input=bad, cwd=ROOT, capture_output=True,
                          timeout=10, env={"PYTHONPATH": str(ROOT / "src")})
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    response = proc.stdout.splitlines()
    assert len(response) == 1
    assert __import__("json").loads(response[0])["error"]["code"] == -32700


def test_sdk_recorded_tool_stdio_modern(tmp_path):
    """MCP real stdio -> recorded adapter -> SQLite; requires SDK installation."""
    import anyio
    from mcp import Client, StdioServerParameters
    from verified_execution.ledger import OperationLedger

    database = tmp_path / "mcp-ledger.sqlite3"
    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=["-u", str(ROOT / "server_mcp.py")], cwd=ROOT,
            env={"PYTHONPATH": str(ROOT / "src"),
                 "VERIFIED_EXECUTION_LEDGER_PATH": str(database)})
        async with Client(params, mode="2026-07-28") as client:
            listed = await client.list_tools()
            assert {item.name for item in listed.tools} == {"checked_add", "checked_add_recorded"}
            first = await client.call_tool("checked_add_recorded", {
                "request_key": "logical-001", "left": 12, "right": 30})
            assert not first.is_error and first.structured_content["result"] == 42
            operation_id = first.structured_content["operation_id"]
            replay = await client.call_tool("checked_add_recorded", {
                "request_key": "logical-001", "left": 12, "right": 30})
            assert not replay.is_error and replay.structured_content == {
                "result": 42, "operation_id": operation_id, "replayed": True}
            conflict = await client.call_tool("checked_add_recorded", {
                "request_key": "logical-001", "left": 12, "right": 31})
            assert conflict.is_error
            malformed = await client.call_tool("checked_add_recorded", {
                "request_key": "logical-002", "left": 1, "right": 2, "extra": 3})
            assert malformed.is_error
        # MCP-LEDGER-001: default recorded mode is lease-fenced, not legacy dispatch.
        # Exact history also proves replay did not cause another dispatch.
        assert OperationLedger(database).history(operation_id) == [
            "reserved", "leased_dispatch", "leased_completed",
        ]
    anyio.run(run)


def test_sdk_recorded_tool_unauthorized_in_process(tmp_path):
    """A host with no capability cannot be escalated through tool arguments."""
    import anyio
    from mcp import Client
    from server_mcp import build_server
    from verified_execution.core import LocalAuthority
    async def run():
        denied = build_server(LocalAuthority(frozenset()),
                              ledger_path=str(tmp_path / "denied.sqlite3"))
        async with Client(denied, mode="2026-07-28") as client:
            response = await client.call_tool("checked_add_recorded", {
                "request_key": "denied-001", "left": 1, "right": 2})
            assert response.is_error
    anyio.run(run)


def test_sdk_recorded_tool_guarded_stdio(tmp_path):
    """Real MCP SDK through the bounded-JSON guard into persisted tool."""
    import anyio
    from mcp import Client, StdioServerParameters
    from verified_execution.ledger import OperationLedger
    database = tmp_path / "guarded.sqlite3"
    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-u", str(ROOT / "scripts" / "guarded_stdio.py"),
                  "--max-bytes", "4096", "--", sys.executable, "-u",
                  str(ROOT / "server_mcp.py")],
            cwd=ROOT,
            env={"PYTHONPATH": str(ROOT / "src"),
                 "VERIFIED_EXECUTION_LEDGER_PATH": str(database)},
        )
        async with Client(params, mode="2026-07-28") as client:
            response = await client.call_tool("checked_add_recorded", {
                "request_key": "guard-001", "left": 20, "right": 22})
            assert not response.is_error and response.structured_content["result"] == 42
            operation_id = response.structured_content["operation_id"]
        # MCP-LEDGER-001: default recorded mode is lease-fenced, not legacy dispatch.
        # Exact history also proves replay did not cause another dispatch.
        assert OperationLedger(database).history(operation_id) == [
            "reserved", "leased_dispatch", "leased_completed",
        ]
    anyio.run(run)


@pytest.mark.parametrize('era', ['2026-07-28', 'legacy'])
def test_sdk_in_process_request_cancellation_after_dispatch(tmp_path, era):
    """Real SDK must propagate cancellation and suppress response of cancelled call.

    When SDK is unavailable, both test cases are SKIPPED, never reported PASS.
    A timed-out test does not establish protocol conformance.
    """
    import anyio
    from mcp import Client
    from server_mcp import build_server
    from verified_execution.worker_leases import LeasedLedger
    db_path = tmp_path / 'wire-cancel.sqlite3'

    async def run():
        started = anyio.Event()
        async def after(_op):
            started.set()
            await anyio.Event().wait()
        server = build_server(ledger_path=str(db_path), after_dispatch=after)
        async with Client(server, mode=era) as client:
            got_reply = []
            scope = anyio.CancelScope()
            async def invoke():
                with scope:
                    reply = await client.call_tool('checked_add_recorded', {
                        'request_key':'wire-cancel-1','left':15,'right':27})
                    got_reply.append(reply)
            with anyio.fail_after(6):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(invoke)
                    await started.wait()
                    scope.cancel()
            assert not got_reply, 'SDK sent a forbidden response to cancelled request'
        db = LeasedLedger(db_path)
        with db._open() as conn:
            row = conn.execute("SELECT state,cancel_requested FROM operations WHERE request_key='wire-cancel-1'").fetchone()
        assert row == ('unknown', 1)

    anyio.run(run)


@pytest.mark.parametrize('era', ['2026-07-28', 'legacy'])
def test_sdk_in_process_cancel_after_commit_keeps_result(tmp_path, era):
    """A client may cancel a response even after durable success committed."""
    import anyio
    from mcp import Client
    from server_mcp import build_server
    from verified_execution.worker_leases import LeasedLedger
    db_path = tmp_path / 'wire-success.sqlite3'

    async def run():
        ready = anyio.Event()
        async def after_commit(_op):
            ready.set()
            await anyio.Event().wait()
        server=build_server(ledger_path=str(db_path), after_commit=after_commit)
        async with Client(server, mode=era) as client:
            responses=[]
            scope=anyio.CancelScope()
            async def invoke():
                with scope:
                    responses.append(await client.call_tool('checked_add_recorded', {
                        'request_key':'wire-committed','left':40,'right':2}))
            with anyio.fail_after(6):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(invoke)
                    await ready.wait()
                    scope.cancel()
            assert not responses
        db=LeasedLedger(db_path)
        with db._open() as conn:
            row=conn.execute("SELECT operation_id,state FROM operations WHERE request_key='wire-committed'").fetchone()
        assert row is not None and row[1]=='succeeded'
        assert db.read_calculation(row[0], principal='local-process')==42

    anyio.run(run)
