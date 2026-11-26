"""Regression checks for the supported local runtime."""
import json
from pathlib import Path
import subprocess
import sys
from threading import Event
from time import time_ns
import tomllib

import anyio
import pytest

from verified_execution.core import LocalAuthority
from verified_execution.ledger import HostPrincipal, LedgerError, OperationLedger, ResourceSnapshot
from verified_execution.request_limits import BoundedReadOnlyExecutor, CapacityExhausted, ReadOnlyPolicy
from verified_execution.worker_leases import LeasedLedger

ROOT = Path(__file__).resolve().parents[1]


def test_core_import_and_python_version_in_isolation():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["requires-python"] == ">=3.13"
    pinned = tuple(map(int, (ROOT / ".python-version").read_text().strip().split(".")))
    assert len(pinned) == 3 and pinned >= (3, 13, 0)
    # -I -S excludes user paths, site packages, and the installed MCP SDK.
    source = """
import json, sys
sys.path.insert(0, sys.argv[1])
from verified_execution.core import CalculationService, LocalAuthority
r = CalculationService(LocalAuthority.calculator_only()).execute("checked_add", {"left": 29, "right": 13})
print(json.dumps([list(sys.version_info[:3]), r.result, "mcp" in sys.modules]))
"""
    version, value, imported_sdk = json.loads(subprocess.check_output(
        [sys.executable, "-I", "-S", "-c", source, str(ROOT / "src")], cwd=ROOT))
    assert tuple(version) >= (3, 13, 0) and (value, imported_sdk) == (42, False)


def test_wait_for_slot_and_reject_while_timed_out_worker_finishes():
    entered, leave, exited = Event(), Event(), Event()
    called = []
    def slow_call():
        entered.set()
        try:
            leave.wait(3)
        finally:
            exited.set()
    async def run():
        executor = BoundedReadOnlyExecutor(ReadOnlyPolicy(timeout_ms=160, max_concurrent=1))
        async def first():
            with pytest.raises(TimeoutError):
                await executor.run(slow_call)
        async def waiting():
            with pytest.raises(TimeoutError):
                await executor.run(lambda: called.append("queued"))
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(first)
                assert await anyio.to_thread.run_sync(entered.wait, 1)
                group.start_soon(waiting)
                await anyio.sleep(0.02)
                assert not called and not exited.is_set()
            with pytest.raises(CapacityExhausted):
                await executor.run(lambda: called.append("extra"))
        finally:
            leave.set()
            assert await anyio.to_thread.run_sync(exited.wait, 1)
        await executor.run(lambda: called.append("after"))
    anyio.run(run)
    assert called == ["after"]


def test_revoked_approval_stays_revoked_after_restart(tmp_path):
    path = tmp_path / "history.sqlite3"
    ledger = OperationLedger(path)
    args = dict(principal="owner", tool_name="write_value", target_id="r",
                resource_version="one", arguments={"value": 8})
    op = ledger.reserve(**args, request_key="one")
    ledger.issue_approval(**args, approval_id="one", expires_at_ms=time_ns() // 1_000_000 + 60_000)
    assert ledger.revoke_approval("one")
    assert not ledger.revoke_approval("one")
    with pytest.raises(LedgerError, match="APPROVAL_INVALID"):
        OperationLedger(path).begin_dispatch(op.operation_id, approval_id="one")
    assert ledger.history(op.operation_id) == ["reserved"]


@pytest.mark.parametrize("leased", [False, True], ids=["legacy", "lease"])
def test_dispatch_checks_live_host_grant_after_version_read(tmp_path, leased):
    revoked = Event()
    def current_version(_target):
        revoked.set()
        return ResourceSnapshot("owner", "one")
    db = (LeasedLedger if leased else OperationLedger)(tmp_path / "ledger.db", read_resource=current_version)
    authority = LocalAuthority(frozenset({"checked_add"}), revocation_event=revoked)
    op = db.reserve(principal="owner", tool_name="checked_add", target_id="r", resource_version="one",
                    arguments={"left": 1, "right": 2}, request_key="one", requires_approval=False)
    assert authority.permits("checked_add")
    with pytest.raises(LedgerError, match="NOT_AUTHORIZED"):
        if leased:
            db.start(op.operation_id, token=db.new_session().token, authority=authority)
        else:
            db.begin_dispatch(op.operation_id, authority=authority)
    assert db.history(op.operation_id) == ["reserved"]


def test_approval_cannot_be_transferred_to_another_principal(tmp_path):
    db = OperationLedger(tmp_path / "ledger.db")
    args = dict(tool_name="write_value", target_id="r", resource_version="one", arguments={"value": 8})
    op = db.reserve(**args, request_key="write", principal="second")
    db.issue_approval(**args, approval_id="grant", principal="first", expires_at_ms=time_ns() // 1_000_000 + 60_000)
    with pytest.raises(LedgerError, match="APPROVAL_INVALID"):
        db.begin_dispatch(op.operation_id, approval_id="grant")
    assert db.history(op.operation_id) == ["reserved"]


def test_invalid_host_identity_rejected_before_database_creation(tmp_path):
    pytest.importorskip("mcp")
    from server_mcp import build_server
    from verified_execution.core import LOCAL_PUBLISHER
    host = HostPrincipal("local-user")
    assert host.name == "local-user" and host.name != LOCAL_PUBLISHER
    path = tmp_path / "unused.sqlite3"
    with pytest.raises(LedgerError, match="INVALID_PRINCIPAL"):
        build_server(principal="", ledger_path=str(path))
    assert not path.exists()


def test_read_grant_cannot_start_approved_write(tmp_path):
    db = OperationLedger(tmp_path / "ledger.db")
    args = dict(principal="owner", tool_name="write_value", target_id="r", resource_version="one",
                arguments={"value": 8})
    op = db.reserve(**args, request_key="write")
    db.issue_approval(**args, approval_id="grant", expires_at_ms=time_ns() // 1_000_000 + 60_000)
    with pytest.raises(LedgerError, match="NOT_AUTHORIZED"):
        db.begin_dispatch(op.operation_id, approval_id="grant", authority=LocalAuthority.calculator_only())
    assert db.history(op.operation_id) == ["reserved"]
    writer = LocalAuthority(frozenset({"write_value"}))
    assert db.begin_dispatch(op.operation_id, approval_id="grant", authority=writer).state == "dispatched"


def test_same_named_local_tool_with_different_contents_fails_before_import(tmp_path):
    import shutil
    policy = json.loads((ROOT / "config/local_tool.json").read_text())
    for relative in ["scripts/run_local_mcp.py", "config/local_tool.json", *policy["sha256"]]:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    (tmp_path / "src/verified_execution/core.py").write_text('raise AssertionError("unexpected import")')
    proc = subprocess.run([sys.executable, "-I", str(tmp_path / "scripts/run_local_mcp.py")],
                          capture_output=True, timeout=5)
    assert (proc.returncode, proc.stdout, proc.stderr) == (1, b"", b"LOCAL_TOOL_REJECTED\n")


def test_external_destination_grants_do_not_transfer():
    local = LocalAuthority.calculator_only()
    assert not local.permits("checked_add", destination="service-one")
    configured = LocalAuthority(frozenset({"checked_add"}), allowed_destinations=frozenset({"service-one"}))
    assert configured.permits("checked_add", destination="service-one")
    assert not configured.permits("checked_add", destination="service-two")
    assert not configured.permits("another_tool", destination="service-one")
