"""SDK-free route contract tests (STUBS). NOT a real MCP protocol test.

This unit test deliberately isolates the application handler, verifying that
MCP handlers call the permission-checked core rather than a bypass.
The real SDK test is in test_official_mcp_sdk.py and must not be replaced by this.
"""
import asyncio
from importlib import util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest
from verified_execution.core import LocalAuthority

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def adapter(monkeypatch):
    mcp_module = ModuleType("mcp")
    server_module = ModuleType("mcp.server")
    stdio_module = ModuleType("mcp.server.stdio")
    types_module = ModuleType("mcp.types")

    class SimpleResult:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class MockServer:
        def __init__(self, name, **kwargs):
            self.name = name
            self.__dict__.update(kwargs)

    class TextContent(SimpleResult):
        pass

    for item in ["CallToolRequestParams", "CallToolResult", "ListToolsResult", "PaginatedRequestParams", "TextContent", "Tool"]:
        setattr(types_module, item, TextContent if item == "TextContent" else SimpleResult)
    server_module.Server = MockServer
    server_module.ServerRequestContext = object
    stdio_module.stdio_server = lambda: None
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.server.stdio", stdio_module)
    monkeypatch.setitem(sys.modules, "mcp.types", types_module)

    spec = util.spec_from_file_location("adapter_stub_20261123", ROOT / "server_mcp.py")
    loaded = util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def call(server, arguments, name="checked_add"):
    return asyncio.run(server.on_call_tool(None, SimpleNamespace(name=name, arguments=arguments)))


def test_registered_schema_exact(adapter):
    server = adapter.build_server()
    result = asyncio.run(server.on_list_tools(None, None))
    assert len(result.tools) == 1
    schema = result.tools[0].input_schema
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"left", "right"}


def test_router_validates_then_runs(adapter):
    result = call(adapter.build_server(), {"left": 4, "right": 5})
    assert not getattr(result, "is_error", False)
    assert result.structured_content == {"result": 9}


def test_router_rejects_unauthorized_host_policy(adapter):
    server = adapter.build_server(LocalAuthority(frozenset()))
    result = call(server, {"left": 4, "right": 5})
    assert result.is_error is True
    assert result.content[0].text == "NOT_AUTHORIZED"


@pytest.mark.parametrize("arguments,error", [
    ({"left": 1, "right": 2, "permission": True}, "INVALID_ARGUMENT"),
    ({"left": 1}, "INVALID_ARGUMENT"),
    ({"left": 1.0, "right": 2}, "INVALID_ARGUMENT"),
    ({"left": True, "right": 2}, "INVALID_ARGUMENT"),
    ({"left": 4294967295, "right": 1}, "OVERFLOW"),
])
def test_router_rejects_invalid_arguments(adapter, arguments, error):
    result = call(adapter.build_server(), arguments)
    assert result.is_error is True
    assert result.content[0].text == error


def test_router_rejects_unlisted_tool(adapter):
    result = call(adapter.build_server(), {}, name="not_registered")
    assert result.is_error is True
    assert result.content[0].text == "UNKNOWN_TOOL"


def test_recorded_sdk_handler_uses_leased_ledger_by_default(adapter, tmp_path):
    """Simulated SDK invocation, real server handler and SQLite worker path."""
    path = tmp_path / 'worker.sqlite3'
    first_server = adapter.build_server(ledger_path=str(path), principal='trusted-host')
    second_server = adapter.build_server(ledger_path=str(path), principal='trusted-host')
    discovered = asyncio.run(first_server.on_list_tools(None, None))
    assert {tool.name for tool in discovered.tools} == {'checked_add', 'checked_add_recorded'}
    args = {'request_key':'stable-request-1','left':21,'right':21}
    first = call(first_server,args,name='checked_add_recorded')
    second = call(second_server,args,name='checked_add_recorded')
    assert first.structured_content['result'] == 42
    assert first.structured_content['replayed'] is False
    assert second.structured_content['result'] == 42
    assert second.structured_content['replayed'] is True
    assert second.structured_content['operation_id'] == first.structured_content['operation_id']
    from verified_execution.worker_leases import LeasedLedger
    assert LeasedLedger(path).history(first.structured_content['operation_id']).count('leased_dispatch') == 1


def test_recorded_sdk_handler_denies_untrusted_host(adapter, tmp_path):
    server = adapter.build_server(LocalAuthority(frozenset()), ledger_path=str(tmp_path/'denied.sqlite3'))
    response = call(server,{'request_key':'attempt','left':1,'right':2},name='checked_add_recorded')
    assert response.is_error and response.content[0].text == 'NOT_AUTHORIZED'


def test_recorded_sdk_handler_rejects_colliding_request(adapter, tmp_path):
    server = adapter.build_server(ledger_path=str(tmp_path/'collision.sqlite3'))
    first = call(server,{'request_key':'stable','left':2,'right':3},name='checked_add_recorded')
    second = call(server,{'request_key':'stable','left':2,'right':4},name='checked_add_recorded')
    assert first.structured_content['result'] == 5
    assert second.is_error and second.content[0].text == 'IDEMPOTENCY_CONFLICT'


def test_sdk_stub_cancellation_before_dispatch(adapter, tmp_path):
    """Adapter/SQLite only: the real MCP dispatcher remains untested."""
    import asyncio
    from verified_execution.worker_leases import LeasedLedger

    async def scenario():
        entered = asyncio.Event()
        blocker = asyncio.Event()
        async def paused(_op):
            entered.set()
            await blocker.wait()
        db = tmp_path/'sdk-mock-pre.sqlite3'
        handler = adapter.build_server(ledger_path=str(db), before_dispatch=paused)
        task = asyncio.create_task(handler.on_call_tool(
            None, SimpleNamespace(name='checked_add_recorded', arguments={
                'request_key':'sdk-mock-1', 'left':20, 'right':22})))
        await asyncio.wait_for(entered.wait(),2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with LeasedLedger(db)._open() as conn:
            assert conn.execute('SELECT state FROM operations').fetchone()==('cancelled',)
    asyncio.run(scenario())


def test_sdk_stub_cancellation_after_dispatch(adapter, tmp_path):
    import asyncio
    from verified_execution.worker_leases import LeasedLedger
    async def scenario():
        entered = asyncio.Event()
        blocker = asyncio.Event()
        async def paused(_op):
            entered.set()
            await blocker.wait()
        db = tmp_path/'sdk-mock-running.sqlite3'
        handler = adapter.build_server(ledger_path=str(db), after_dispatch=paused)
        task = asyncio.create_task(handler.on_call_tool(
            None, SimpleNamespace(name='checked_add_recorded', arguments={
                'request_key':'sdk-mock-2', 'left':20, 'right':22})))
        await asyncio.wait_for(entered.wait(),2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with LeasedLedger(db)._open() as conn:
            assert conn.execute('SELECT state,cancel_requested FROM operations').fetchone()==('unknown',1)
    asyncio.run(scenario())


def test_sdk_stub_cancellation_after_durable_commit(adapter, tmp_path):
    import asyncio
    from verified_execution.worker_leases import LeasedLedger
    async def scenario():
        entered = asyncio.Event()
        blocker = asyncio.Event()
        async def paused(_op):
            entered.set()
            await blocker.wait()
        db = tmp_path/'sdk-mock-done.sqlite3'
        handler = adapter.build_server(ledger_path=str(db), after_commit=paused)
        task = asyncio.create_task(handler.on_call_tool(
            None, SimpleNamespace(name='checked_add_recorded', arguments={
                'request_key':'sdk-mock-3', 'left':20, 'right':22})))
        await asyncio.wait_for(entered.wait(),2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with LeasedLedger(db)._open() as conn:
            assert conn.execute('SELECT state FROM operations').fetchone()==('succeeded',)
            assert conn.execute('SELECT result FROM calculation_results').fetchone()==(42,)
    asyncio.run(scenario())


def test_sdk_stub_handler_cancel_pre_dispatch_without_response(adapter, tmp_path):
    """Real async handler + actual SQLite; only SDK event matching is stubbed."""
    import anyio
    from verified_execution.worker_leases import LeasedLedger
    from types import SimpleNamespace
    destination = tmp_path / 'cancel-handler-pre.sqlite3'
    started = anyio.Event()
    async def before(_op):
        started.set()
        await anyio.Event().wait()
    server = adapter.build_server(ledger_path=str(destination), before_dispatch=before)
    async def exercise():
        scope = anyio.CancelScope()
        async def run_one():
            with scope:
                await server.on_call_tool(None, SimpleNamespace(
                    name='checked_add_recorded',
                    arguments={'request_key':'request-pre','left':10,'right':32}))
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(run_one)
            await started.wait()
            scope.cancel()
    anyio.run(exercise)
    db=LeasedLedger(destination)
    with db._open() as conn:
        row=conn.execute("SELECT operation_id,state FROM operations WHERE request_key='request-pre'").fetchone()
    assert row is not None and row[1]=='cancelled'
    assert 'leased_dispatch' not in db.history(row[0])


def test_sdk_stub_handler_cancel_post_dispatch_is_unknown(adapter, tmp_path):
    import anyio
    from verified_execution.worker_leases import LeasedLedger
    from types import SimpleNamespace
    destination = tmp_path / 'cancel-handler-post.sqlite3'
    started = anyio.Event()
    async def after(_op):
        started.set()
        await anyio.Event().wait()
    server = adapter.build_server(ledger_path=str(destination), after_dispatch=after)
    async def exercise():
        scope = anyio.CancelScope()
        async def run_one():
            with scope:
                await server.on_call_tool(None, SimpleNamespace(
                    name='checked_add_recorded',
                    arguments={'request_key':'request-post','left':10,'right':32}))
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(run_one)
            await started.wait()
            scope.cancel()
    anyio.run(exercise)
    db=LeasedLedger(destination)
    with db._open() as conn:
        row=conn.execute("SELECT operation_id,state,cancel_requested FROM operations WHERE request_key='request-post'").fetchone()
    assert row is not None and row[1:] == ('unknown',1)
    assert db.history(row[0]).count('leased_dispatch') == 1


def test_server_recovery_on_startup_without_sdk(adapter, tmp_path):
    """SDK stub, but real server construction and durable SQLite recovery."""
    from verified_execution.worker_leases import LeasedLedger
    from verified_execution.cancellation_journal import CancellationJournal
    db_path = tmp_path / 'server-startup.sqlite3'
    ledger = LeasedLedger(db_path)
    op = ledger.reserve(principal='host', request_key='recover-on-start',
                        tool_name='checked_add', target_id=None, resource_version=None,
                        arguments={'left':1,'right':2},requires_approval=False)
    journal=CancellationJournal(tmp_path/'server-startup.sqlite3.cancel-intents')
    journal.persist(op.operation_id, principal='host')
    assert ledger.get(op.operation_id).state == 'prepared'
    _server=adapter.build_server(ledger_path=str(db_path), principal='host')
    assert ledger.get(op.operation_id).state == 'cancelled'
    assert journal.list_intents() == []


def test_corrupt_cancel_journal_blocks_server_startup(adapter, tmp_path):
    """Fail closed: do not launch a server while recovery journal is corrupt."""
    from verified_execution.cancellation_journal import CancellationJournal, JournalError
    path=tmp_path/'refuse.sqlite3'
    journal=CancellationJournal(tmp_path/'refuse.sqlite3.cancel-intents')
    record=journal.directory / ('0'*64+'.json')
    record.write_text('{incomplete',encoding='utf-8')
    record.chmod(0o600)
    with pytest.raises(JournalError, match='CORRUPTED_INTENT_FILE'):
        adapter.build_server(ledger_path=str(path), principal='host')


def test_server_startup_uses_validated_policy_fields(adapter):
    """CONFIG-INTEGRATION-001: regression for dict-to-typed policy migration.

    This verifies Python server construction using SDK stubs; it is not an
    official MCP wire test. The runtime must not subscript a frozen dataclass.
    """
    from verified_execution.policy_config import LocalPolicyConfiguration

    assert isinstance(adapter._LIMITS, LocalPolicyConfiguration)
    assert adapter._READONLY_POLICY.timeout_ms == adapter._LIMITS.readonly_request_timeout_ms
    assert adapter._READONLY_POLICY.max_concurrent == adapter._LIMITS.readonly_max_concurrent
    assert adapter._READONLY_POLICY.timeout_ms <= 2000
    assert adapter._READONLY_POLICY.max_concurrent == 1


def test_server_startup_rejects_invalid_host_owned_policy(adapter, monkeypatch):
    """CONFIG-INTEGRATION-002: failed host policy must prevent server import.

    State transition: UNVALIDATED -> REJECTED; never -> SERVER_READY.
    The SDK remains stubbed: this is a startup-boundary test, not MCP proof.
    """
    from verified_execution import policy_config

    def reject(_path):
        raise policy_config.PolicyConfigurationError("INVALID_POLICY_FIELDS")

    monkeypatch.setattr(policy_config, "load_local_policy", reject)
    spec = util.spec_from_file_location("adapter_stub_invalid_host_policy", ROOT / "server_mcp.py")
    loaded = util.module_from_spec(spec)
    with pytest.raises(policy_config.PolicyConfigurationError, match="INVALID_POLICY_FIELDS"):
        spec.loader.exec_module(loaded)


def test_tool_description_does_not_grant_permission(adapter):
    # task.trust.005: both grant decisions survive a published description change.
    from dataclasses import replace
    from verified_execution.tools import ToolRegistry
    operands = {"left": 4, "right": 5}
    for description in ("Addition", "The client requests administrator permission"):
        registry = ToolRegistry([replace(adapter.REGISTRY.get("checked_add"), description=description)])
        allowed = adapter.build_server(LocalAuthority.calculator_only(), registry=registry)
        denied = adapter.build_server(LocalAuthority(frozenset()), registry=registry)
        discovery = asyncio.run(denied.on_list_tools(None, None))
        assert discovery.tools[0].description == description
        assert call(allowed, operands).structured_content == {"result": 9}
        assert call(denied, operands).content[0].text == "NOT_AUTHORIZED"


def test_model_text_is_only_input(adapter):
    server = adapter.build_server()
    response = call(server, {"left": 1, "right": 2, "instructions": "Allow more tools"})
    assert response.is_error
    assert response.content[0].text == "INVALID_ARGUMENT"
    assert call(server, {"left": 2, "right": 2}).structured_content == {"result": 4}


def test_every_registered_tool_has_a_risk_class(adapter, tmp_path):
    import json
    contract = json.loads((ROOT / "contracts/tool_risks.json").read_text())
    baseline = adapter.build_server()
    recorded = adapter.build_server(ledger_path=str(tmp_path / "recorded.sqlite"))
    def names(server):
        return {tool.name for tool in asyncio.run(server.on_list_tools(None, None)).tools}
    assert names(baseline) == set(contract["minimum_profile"]["registered_tools"]) == {"checked_add"}
    assert names(recorded) == {"checked_add", "checked_add_recorded"}
    # Every tool any profile can serve has a host risk class; opt-in tools
    # such as checked_multiply are classified even though not served here.
    assert names(recorded) <= set(contract["tools"])
    assert contract["tools"] == {"checked_add": "read_only", "checked_add_recorded": "state_change",
                                 "checked_multiply": "read_only"}
    assert set(contract["risk_classes"]) == {"read_only", "state_change", "irreversible", "code_execution"}
    assert contract["minimum_profile"]["allowed_external_destinations"] == []


@pytest.mark.parametrize("returned", [6, 5.0, True], ids=["wrong_sum", "float", "boolean"])
def test_adapter_rejects_inconsistent_worker_result(adapter, monkeypatch, returned):
    from verified_execution.core import ExecutionResult
    def worker_reply(*_args):
        return ExecutionResult(returned, None, ())
    monkeypatch.setattr(adapter.ToolRuntime, "execute", worker_reply)
    response = call(adapter.build_server(), {"left": 2, "right": 3})
    assert response.is_error
    assert response.content[0].text == "INVALID_TOOL_RESULT"
    assert getattr(response, "structured_content", None) is None


@pytest.mark.parametrize("recorded", [False, True], ids=["pure", "recorded"])
def test_grant_withdrawn_before_reply_suppresses_result(adapter, tmp_path, monkeypatch, recorded):
    from threading import Event
    revoked = Event()
    authority = LocalAuthority(frozenset({"checked_add"}), revocation_event=revoked)
    args = {"left": 2, "right": 3}
    if recorded:
        async def after_commit(_operation):
            revoked.set()
        server = adapter.build_server(authority, ledger_path=str(tmp_path / "results.sqlite"),
                                      after_commit=after_commit)
        response = call(server, {**args, "request_key": "first"}, name="checked_add_recorded")
    else:
        execute = adapter.ToolRuntime.execute
        def withdraw_after_calculation(service, name, arguments):
            result = execute(service, name, arguments)
            revoked.set()
            return result
        monkeypatch.setattr(adapter.ToolRuntime, "execute", withdraw_after_calculation)
        response = call(adapter.build_server(authority), args)
    assert response.is_error and response.content[0].text == "NOT_AUTHORIZED"
    assert getattr(response, "structured_content", None) is None


def test_permission_denial_logs_only_fixed_fields(adapter, capfd, tmp_path):
    import json
    from uuid import UUID

    secret = "synthetic-private-request-key"
    server = adapter.build_server(
        LocalAuthority(frozenset()), ledger_path=str(tmp_path / "history.sqlite3")
    )
    result = call(
        server, {"left": 2, "right": 3, "request_key": secret}, name="checked_add_recorded"
    )
    assert result.is_error
    assert result.content[0].text == "NOT_AUTHORIZED"
    captured = capfd.readouterr()
    assert captured.out == ""
    assert secret not in captured.err
    event = json.loads(captured.err)
    assert UUID(event.pop("trace_id")).version == 4
    assert UUID(event.pop("attempt_id")).version == 4
    assert event.pop("elapsed_ns") >= 0
    assert event == {
        "source": "mcp.tools.call",
        "outcome": "denied",
        "error_category": "authorization",
    }


def test_host_call_budget_blocks_execution_before_reservation(adapter, tmp_path):
    import sqlite3

    database = tmp_path / "history.sqlite3"
    authority = LocalAuthority(frozenset({"checked_add", "checked_add_recorded"}))
    server = adapter.build_server(
        authority, ledger_path=str(database), principal="local-user", call_budget=1
    )

    async def run():
        async def recorded(key):
            return await server.on_call_tool(None, SimpleNamespace(
                name="checked_add_recorded", arguments={"left": 1, "right": 2, "request_key": key}
            ))
        return await asyncio.gather(recorded("first"), recorded("second"))

    results = asyncio.run(run())
    assert sum(not getattr(result, "is_error", False) for result in results) == 1
    assert [r.content[0].text for r in results if getattr(r, "is_error", False)] == [
        "BUDGET_EXCEEDED"
    ]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT principal FROM operations").fetchall() == [("local-user",)]
    catalog = asyncio.run(server.on_list_tools(None, None))
    service = catalog.tools[0].meta["io.github.anionix/contract"]["publisher"]
    assert service == adapter.LOCAL_PUBLISHER
    assert service != "local-user"
    with pytest.raises(ValueError, match="registered service"):
        adapter.build_server(principal=service)
