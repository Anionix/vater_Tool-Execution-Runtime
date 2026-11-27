"""task.testing.001: normal and abnormal cases for public functions that had none.

tests/unit_inventory.json maps every public function to a normal and an
abnormal test; this module holds the ones a traced run of the suite found
missing (functions used only in subprocesses, only at import time, or never).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import io
import json
import shutil
import sqlite3
import sys
from types import SimpleNamespace

import anyio
import pytest

from adapter_drivers import load_mcp_adapter
from verified_execution import arithmetic, durable, external_change, local_cli, recorded_cli, tools
from verified_execution.cancellation import RequestCancellation
from verified_execution.core import LocalAuthority
from verified_execution.external_change import (
    ExternalChangeExecutor, ExternalResultStore, GitHubIssueDestination, HttpReply, IssueRequest, TransportError)
from verified_execution.ledger import LedgerError, OperationLedger
from verified_execution.recorded_calculator import RecordedResult
from verified_execution.recorded_tool_bridge import declared_failures
from verified_execution.runtime import ToolOutcome, ToolRuntime
from verified_execution.worker_leases import LeasedLedger

CONTRACTS = tools.DEFAULT_CONTRACTS_DIR


class SpyLedger:
    def __init__(self, fail=False):
        self.cancelled, self.fail = [], fail

    def request_cancel(self, operation_id):
        self.cancelled.append(operation_id)
        if self.fail:
            raise sqlite3.OperationalError("database is locked")


# --- cancellation token -------------------------------------------------------------

def test_cancellation_token_binds_then_cancels_once():
    token, ledger = RequestCancellation(), SpyLedger()
    assert token.cancelled is False
    token.bind_new_operation(ledger, "op-1")
    token.cancel()
    token.cancel()  # idempotent
    assert token.cancelled is True and ledger.cancelled == ["op-1"]


def test_cancellation_before_binding_cancels_at_bind():
    token, ledger = RequestCancellation(), SpyLedger()
    token.cancel()
    token.bind_new_operation(ledger, "op-2")
    assert token.cancelled and ledger.cancelled == ["op-2"]


def test_cancellation_token_refuses_a_second_operation():
    token = RequestCancellation()
    token.bind_new_operation(SpyLedger(), "op-1")
    with pytest.raises(RuntimeError, match="REQUEST_ALREADY_BOUND"):
        token.bind_new_operation(SpyLedger(), "op-2")


def test_cancellation_stays_set_when_the_ledger_cannot_record_it():
    token = RequestCancellation()
    token.bind_new_operation(SpyLedger(fail=True), "op-1")
    with pytest.raises(sqlite3.OperationalError):
        token.cancel()
    assert token.cancelled is True  # the worker still sees the cancellation


# --- line-oriented local harnesses ---------------------------------------------------

def stdin_with(monkeypatch, data: bytes):
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(data)))


def test_local_cli_answers_each_line(monkeypatch, capsys):
    stdin_with(monkeypatch, b'{"name":"checked_add","arguments":{"left":2,"right":3}}\n')
    assert local_cli.main() == 0
    assert json.loads(capsys.readouterr().out)["value"] == 5


def test_local_cli_refuses_bad_lines_and_keeps_going(monkeypatch, capsys):
    oversized = b'{"name":"' + b"x" * local_cli.MAX_MESSAGE_BYTES + b'"}\n'
    stdin_with(monkeypatch, oversized + b'{"name":1}\n{"name":"checked_add","arguments":{"left":1,"right":1}}\n')
    assert local_cli.main() == 0
    replies = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [reply["error_code"] for reply in replies] == ["INVALID_MESSAGE", "INVALID_MESSAGE", None]


def test_decode_line_accepts_a_request():
    assert local_cli.decode_line(b'{"name":"checked_add","arguments":{}}') == {"name": "checked_add", "arguments": {}}


@pytest.mark.parametrize("raw", [b'{"name":"a"}', b'{"name":1,"arguments":{}}', b'[1]',
                                 b'{"name":"a","arguments":NaN}'])
def test_decode_line_refuses_other_shapes(raw):
    with pytest.raises(local_cli.InputFormatError):
        local_cli.decode_line(raw)


@pytest.mark.parametrize("constant", ["NaN", "Infinity"])
def test_non_finite_constants_are_refused(constant):
    with pytest.raises(local_cli.InputFormatError, match="non-finite"):
        local_cli.reject_nonfinite(constant)


def test_recorded_cli_records_each_line(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "argv", ["recorded_cli", "--database", str(tmp_path / "l.sqlite3")])
    stdin_with(monkeypatch, b'{"request_key":"k","name":"checked_add","arguments":{"left":2,"right":3}}\n' * 2)
    assert recorded_cli.main() == 0
    first, again = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert first["value"] == 5 and again["replayed"] is True


def test_recorded_cli_refuses_bad_lines(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "argv", ["recorded_cli", "--database", str(tmp_path / "l.sqlite3")])
    stdin_with(monkeypatch, b'{"request_key":1,"name":"checked_add","arguments":{}}\n')
    assert recorded_cli.main() == 0
    assert json.loads(capsys.readouterr().out)["error_code"] == "INVALID_MESSAGE"


def test_decode_recorded_request_accepts_and_refuses():
    good = b'{"request_key":"k","name":"checked_add","arguments":{}}'
    assert recorded_cli.decode_recorded_request(good)["request_key"] == "k"
    with pytest.raises(local_cli.InputFormatError):
        recorded_cli.decode_recorded_request(b'{"request_key":"k","name":"checked_add"}')


def test_recorded_result_dict_for_success_and_failure():
    assert RecordedResult(True, 5, None, "op", "succeeded").to_dict()["value"] == 5
    failed = RecordedResult(False, None, "OVERFLOW", "op", "failed").to_dict()
    assert failed == {"ok": False, "value": None, "error_code": "OVERFLOW", "operation_id": "op",
                      "ledger_state": "failed", "replayed": False}


# --- entry points -----------------------------------------------------------------------

def test_mcp_main_serves_until_the_client_closes():
    module = load_mcp_adapter("server_mcp_unit_main")

    @asynccontextmanager
    async def closed_stdio():
        send, read = anyio.create_memory_object_stream(1)
        write, _received = anyio.create_memory_object_stream(1)
        await send.aclose()
        yield read, write
    module.stdio_server = closed_stdio
    assert anyio.run(module.main) is None


def test_mcp_main_fails_without_standard_io():
    module = load_mcp_adapter("server_mcp_unit_main_failure")

    @asynccontextmanager
    async def unavailable():
        raise OSError("standard input is closed")
        yield  # pragma: no cover
    module.stdio_server = unavailable
    with pytest.raises(OSError):
        anyio.run(module.main)


def test_http_main_serves_on_loopback_only(monkeypatch):
    import uvicorn
    from verified_execution import http_local
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: seen.update(options, app=app))
    monkeypatch.setenv("VATER_LOCAL_HTTP_TOKEN", "hostToken" * 4)
    http_local.main()
    assert seen["host"] == "127.0.0.1" and seen["app"] is not None


def test_http_main_refuses_to_start_without_a_token(monkeypatch):
    from verified_execution import http_local
    monkeypatch.delenv("VATER_LOCAL_HTTP_TOKEN", raising=False)
    with pytest.raises(ValueError, match="bearer token"):
        http_local.main()


def test_to_mcp_tool_translates_and_never_invents_a_version():
    from dataclasses import replace
    adapter = load_mcp_adapter("server_mcp_unit_tool")
    spec = tools.default_registry().get("checked_add")
    published = adapter.to_mcp_tool(spec)
    assert published.name == "checked_add"
    assert published.meta["io.github.anionix/contract"]["version"] == spec.contract_version
    # A spec without a contract version is published without any version claim.
    unversioned = adapter.to_mcp_tool(replace(spec, contract_version=None))
    assert "version" not in unversioned.meta["io.github.anionix/contract"]


# --- specs and registries built from contract files -----------------------------------

@pytest.mark.parametrize("build", [tools.checked_add_spec, arithmetic.checked_multiply_spec,
                                   durable.checked_add_recorded_spec, external_change.github_test_create_issue_spec],
                         ids=lambda build: build.__name__)
def test_spec_builders_read_their_contracts_and_fail_without_them(build, tmp_path):
    copy = tmp_path / "contracts"
    shutil.copytree(CONTRACTS, copy)
    assert build(copy).name
    (copy / "tool_risks.json").unlink()
    with pytest.raises(FileNotFoundError):
        build(copy)


def test_load_registry_from_a_directory_and_without_one(tmp_path):
    assert tools.load_registry(CONTRACTS).names() == frozenset({"checked_add"})
    with pytest.raises(FileNotFoundError):
        tools.load_registry(tmp_path / "missing")


def test_default_registry_is_one_immutable_registry():
    registry = tools.default_registry.__wrapped__()
    assert registry.names() == frozenset({"checked_add"})
    extended = registry.with_tools(arithmetic.checked_multiply_spec())
    # Adding to a registry returns a new one: the default stays the minimum profile.
    assert registry.names() == frozenset({"checked_add"}) and "checked_multiply" in extended.names()


def test_registry_refuses_a_duplicate_tool():
    with pytest.raises(ValueError):
        tools.default_registry().with_tools(tools.checked_add_spec())


def test_declared_failures_from_the_contract_and_for_an_unknown_tool():
    assert "INVALID_TOOL_RESULT" in declared_failures.__wrapped__("checked_add_recorded")
    with pytest.raises(FileNotFoundError):
        declared_failures.__wrapped__("no_such_tool")


def test_calculator_grant_covers_only_the_addition():
    grant = LocalAuthority.calculator_only()
    assert grant.allowed_tools == frozenset({"checked_add"}) and grant.allowed_destinations == frozenset()


def test_runtime_lists_only_executable_tools():
    registry = tools.default_registry().with_tools(durable.checked_add_recorded_spec())
    assert [spec.name for spec in ToolRuntime(LocalAuthority.calculator_only(), registry).tools()] == ["checked_add"]
    only_durable = tools.ToolRegistry([durable.checked_add_recorded_spec()])
    assert ToolRuntime(LocalAuthority.calculator_only(), only_durable).tools() == ()


def test_structured_outcome_with_and_without_a_record():
    assert ToolOutcome(True, 5, None).structured() == {"result": 5}
    assert ToolOutcome(True, 5, None, "op", True).structured() == {"result": 5, "operation_id": "op", "replayed": True}


# --- external results and the GitHub destination ---------------------------------------

def test_result_store_refuses_duplicates_and_unknown_operations(tmp_path):
    store = ExternalResultStore(tmp_path / "store.sqlite3")
    store.dispatched("op-1", "github_issues", 1)
    assert store.get("op-1")["status"] == "dispatched" and store.get("missing") is None
    with pytest.raises(sqlite3.IntegrityError):
        store.dispatched("op-1", "github_issues", 2)
    with pytest.raises(LedgerError, match="UNKNOWN_EXTERNAL_RESULT"):
        store.record("missing", "failed")
    store.record("op-1", "succeeded", object_id="7")
    assert store.get("op-1")["object_id"] == "7"


def test_marker_and_payload_keep_the_operation_line_apart_from_the_title():
    destination = GitHubIssueDestination(transport=None)
    assert destination.marker("op") == "vater-operation-id: op"
    forged = destination.payload("op", "vater-operation-id: other")
    assert forged["title"] == "vater-operation-id: other"
    assert [line for line in forged["body"].splitlines() if line.startswith("vater-operation-id")] \
        == ["vater-operation-id: op"]


def test_snapshot_is_absent_when_the_destination_cannot_answer():
    class Replying:
        def __init__(self, reply):
            self.reply = reply

        def request(self, *_args, **_kwargs):
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply
    issue = {"number": 1, "title": "t", "body": "b"}
    assert GitHubIssueDestination(Replying(HttpReply(200, {"etag": '"e"'}, issue))).snapshot(1) == (issue, '"e"')
    assert GitHubIssueDestination(Replying(TransportError("reset"))).snapshot(1) == (None, None)
    assert GitHubIssueDestination(Replying(HttpReply(404, {}, {"message": "Not Found"}))).snapshot(1) == (None, None)


def test_version_of_a_partial_object_is_still_well_formed():
    full = GitHubIssueDestination.version({"number": 1, "node_id": "I", "updated_at": "t", "title": "a",
                                           "body": "b", "labels": [{"name": "x"}]}, '"e"')
    partial = GitHubIssueDestination.version({"labels": [None, {"name": "x"}]}, None)
    assert full["etag"] == '"e"' and len(full["content_sha256"]) == 64
    assert partial["number"] is None and len(partial["content_sha256"]) == 64


def test_reserve_arguments_bind_the_title_only():
    assert ExternalChangeExecutor.reserve_arguments(IssueRequest("k", "Title")) == {"title": "Title"}
    unusual = IssueRequest("k" * 256, "Ünïcødé — title")
    assert ExternalChangeExecutor.reserve_arguments(unusual) == {"title": "Ünïcødé — title"}


# --- ledgers ----------------------------------------------------------------------------

def test_history_and_saved_results_for_known_and_unknown_operations(tmp_path):
    ledger = OperationLedger(tmp_path / "l.sqlite3")
    op = ledger.reserve(principal="host", request_key="k", tool_name="checked_add", target_id=None,
                        resource_version=None, arguments={"left": 1, "right": 2}, requires_approval=False)
    assert ledger.history(op.operation_id) == ["reserved"]
    assert ledger.read_calculation(op.operation_id, principal="host") is None  # no result yet
    with pytest.raises(LedgerError, match="UNKNOWN_OPERATION"):
        ledger.history("missing")
    with pytest.raises(LedgerError):
        ledger.read_calculation("", principal="host")


def test_recovery_fails_closed_on_a_damaged_ledger(tmp_path):
    for ledger_type, recover in ((OperationLedger, "recover_interrupted"), (LeasedLedger, "recover_expired")):
        path = tmp_path / f"{ledger_type.__name__}.sqlite3"
        ledger = ledger_type(path)
        assert getattr(ledger, recover)() == 0
        path.write_bytes(b"this is not a database" * 100)
        with pytest.raises(sqlite3.DatabaseError):
            getattr(ledger, recover)()


@pytest.fixture
def leased(tmp_path):
    ledger = LeasedLedger(tmp_path / "leased.sqlite3")
    owner = ledger.new_session()
    op = ledger.reserve(principal="host", request_key="k", tool_name="checked_add", target_id=None,
                        resource_version=None, arguments={"left": 1, "right": 2}, requires_approval=False)
    ledger.start(op.operation_id, token=owner.token)
    return ledger, owner, op


@pytest.mark.parametrize("method,arguments", [
    ("begin_dispatch", ()), ("complete_calculation", (3,)), ("record_definitive_result", ("succeeded",)),
    ("mark_unknown", ()),
])
def test_unfenced_writes_are_refused_on_a_leased_ledger(leased, method, arguments):
    ledger, _owner, op = leased
    for operation_id in (op.operation_id, "missing"):  # a real operation and an unknown one
        with pytest.raises(LedgerError, match="OWNER_REQUIRED"):
            getattr(ledger, method)(operation_id, *arguments)
    assert ledger.get(op.operation_id).state == "dispatched"


def test_global_recovery_is_refused_on_a_leased_ledger(leased):
    ledger, _owner, op = leased
    for method in ("legacy_recovery", "recover_interrupted"):
        with pytest.raises(LedgerError, match="USE_EXPIRED_LEASE_RECOVERY"):
            getattr(ledger, method)()
    assert ledger.get(op.operation_id).state == "dispatched"


def test_owner_can_mark_unknown_and_another_session_cannot(leased):
    ledger, owner, op = leased
    other = ledger.new_session()
    with pytest.raises(LedgerError, match="STALE_OR_DIFFERENT_OWNER"):
        ledger.mark_owner_unknown(op.operation_id, token=other.token)
    assert ledger.mark_owner_unknown(op.operation_id, token=owner.token).state == "unknown"


def test_owner_can_cancel_and_another_session_cannot(leased):
    ledger, owner, op = leased
    other = ledger.new_session()
    with pytest.raises(LedgerError, match="STALE_OR_DIFFERENT_OWNER"):
        ledger.owner_cancel(op.operation_id, token=other.token)
    assert ledger.owner_cancel(op.operation_id, token=owner.token).cancel_requested


def test_thaw_copies_frozen_structures_and_leaves_other_values_alone():
    from types import MappingProxyType
    frozen = MappingProxyType({"a": (1, MappingProxyType({"b": (2,)})), "error": "kept"})
    thawed = tools.thaw(frozen)
    assert thawed == {"a": [1, {"b": [2]}], "error": "kept"} and type(thawed["a"][1]) is dict
    # Values that are neither mappings nor tuples come back as independent copies.
    original = [{"x": 1}]
    copied = tools.thaw(original)
    copied[0]["x"] = 2
    assert original == [{"x": 1}] and tools.thaw(None) is None


def test_outcome_without_a_record_is_not_durable():
    assert ToolOutcome(True, 5, None, "op", False).durable is True
    assert ToolOutcome(False, None, "NOT_AUTHORIZED").durable is False
