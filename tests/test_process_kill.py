"""task.testing.010: a process killed with SIGKILL recovers from durable state.

Every child here is stopped by SIGKILL at a chosen point: while its create
request is held open by the destination, before the request leaves, while it
holds a worker lease, and in the middle of a stream of ledger transactions.
Nothing in the child gets a chance to clean up. Recovery uses only what is on
disk, in a fresh process (the test itself).
"""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from verified_execution.core import LocalAuthority
from verified_execution.external_change import (
    TOOL_NAME, ExternalChangeExecutor, ExternalResultStore, GitHubIssueDestination, UrllibTransport)
from verified_execution.ledger import OperationLedger
from verified_execution.recorded_tool_bridge import RECORDED_TOOL_NAME
from verified_execution.worker_leases import LeasedLedger, LeasedToolBridge

pytestmark = pytest.mark.skipif(os.name != "posix", reason="SIGKILL is POSIX-only")

ROOT = Path(__file__).resolve().parents[1]
REPO = "example/repository"
PREFIX = f"/repos/{REPO}"


class FakeIssues(BaseHTTPRequestHandler):
    """A real HTTP GitHub stand-in. A create can be held open until released."""

    def _reply(self, status, payload, headers=()):
        data = json.dumps(payload).encode()
        try:
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass  # the client was killed while we held its request

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        state = self.server.state
        with state["lock"]:
            number = len(state["issues"]) + 1
            issue = {"number": number, "node_id": f"I_{number}", "title": body["title"], "body": body["body"],
                     "labels": [{"name": name} for name in body["labels"]], "updated_at": "2026-11-26T00:00:00Z"}
            state["issues"].append(issue)
            state["posts"] += 1
        state["received"].set()
        if state["hold"]:
            state["release"].wait(30)
        self._reply(201, issue)

    def do_GET(self):
        url, state = urlsplit(self.path), self.server.state
        if url.path == f"{PREFIX}/issues":
            page = int(parse_qs(url.query)["page"][0])
            with state["lock"]:
                issues = list(state["issues"]) if page == 1 else []
            return self._reply(200, issues)
        number = int(url.path.rsplit("/", 1)[1])
        with state["lock"]:
            issue = state["issues"][number - 1]
        self._reply(200, issue, [("ETag", f'W/"etag-{number}"')])

    def log_message(self, *args):
        pass


@contextmanager
def fake_github(*, hold):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeIssues)
    server.daemon_threads = True
    server.state = {"lock": threading.Lock(), "issues": [], "posts": 0, "hold": hold,
                    "received": threading.Event(), "release": threading.Event()}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.state["release"].set()
        server.shutdown()
        server.server_close()


def spawn(code: str, *args) -> subprocess.Popen:
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    return subprocess.Popen([sys.executable, "-u", "-c", code, *map(str, args)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def kill(child: subprocess.Popen) -> None:
    child.send_signal(signal.SIGKILL)
    assert child.wait(timeout=5) == -signal.SIGKILL
    child.stdout.close()
    child.stderr.close()


def approver(ledger):
    counter = iter(range(1, 1000))

    def approve(op, arguments):
        approval_id = f"approval-{op.operation_id}-{next(counter)}"
        ledger.issue_approval(approval_id=approval_id, principal=op.principal, tool_name=op.tool_name,
                              target_id=op.target_id, resource_version=op.resource_version,
                              arguments=arguments, expires_at_ms=int(time.time() * 1000) + 60_000)
        return approval_id
    return approve


EXTERNAL_CHILD = """
import sys, time
from verified_execution.core import LocalAuthority
from verified_execution.external_change import (TOOL_NAME, ExternalChangeExecutor, ExternalResultStore,
    GitHubIssueDestination, UrllibTransport)
from verified_execution.ledger import OperationLedger
path, base, mode = sys.argv[1:4]
ledger = OperationLedger(path)

def approve(op, arguments):
    ledger.issue_approval(approval_id='child-' + op.operation_id, principal=op.principal,
        tool_name=op.tool_name, target_id=op.target_id, resource_version=op.resource_version,
        arguments=arguments, expires_at_ms=int(time.time() * 1000) + 60_000)
    return 'child-' + op.operation_id

class Stalled:
    def request(self, method, path, **kwargs):
        print('about-to-send', flush=True)
        time.sleep(60)

transport = UrllibTransport('example/repository', 'dummy-token', base_url=base) if mode == 'send' else Stalled()
executor = ExternalChangeExecutor(ledger, ExternalResultStore(path), GitHubIssueDestination(transport),
    principal='host', authority=LocalAuthority(frozenset({TOOL_NAME})), target_id='example/repository',
    approve=approve)
executor.call(TOOL_NAME, {'request_key': 'killed', 'title': 'Killed mid-request'})
"""


def restarted_executor(path, base):
    ledger = OperationLedger(path)
    return ledger, ExternalChangeExecutor(
        ledger, ExternalResultStore(path),
        GitHubIssueDestination(UrllibTransport(REPO, "dummy-token", base_url=base)),
        principal="host", authority=LocalAuthority(frozenset({TOOL_NAME})), target_id=REPO, approve=approver(ledger))


def test_killed_while_the_destination_holds_the_create(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with fake_github(hold=True) as server:
        base = f"http://127.0.0.1:{server.server_port}"
        child = spawn(EXTERNAL_CHILD, path, base, "send")
        try:
            assert server.state["received"].wait(10), "the child never sent its create"
        finally:
            kill(child)
        server.state["release"].set()
        ledger, executor = restarted_executor(path, base)
        assert ledger.recover_interrupted() == 1  # dispatched survived the kill
        result = executor.call(TOOL_NAME, {"request_key": "killed", "title": "Killed mid-request"})
        posts = server.state["posts"]
    assert (result.ok, result.value, result.replayed) == (True, 1, True)
    assert posts == 1  # recovered by lookup, never resent
    assert ledger.history(result.operation_id) == ["reserved", "dispatched", "recovered_as_unknown"]
    assert executor.result(result.operation_id)["status"] == "reconciled"


def test_killed_before_the_request_left(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with fake_github(hold=False) as server:
        base = f"http://127.0.0.1:{server.server_port}"
        child = spawn(EXTERNAL_CHILD, path, base, "stall")
        try:
            assert child.stdout.readline().strip() == "about-to-send"
        finally:
            kill(child)
        ledger, executor = restarted_executor(path, base)
        assert ledger.recover_interrupted() == 1
        result = executor.call(TOOL_NAME, {"request_key": "killed", "title": "Killed mid-request"})
        posts = server.state["posts"]
    # Absence in the list is not proof that nothing was created: a person decides.
    assert result.error_code == "HUMAN_RECONCILIATION_REQUIRED" and posts == 0
    assert ledger.get(result.operation_id).state == "unknown"


LEASE_CHILD = """
import sys, time
from verified_execution.worker_leases import LeasedLedger
ledger = LeasedLedger(sys.argv[1])
session = ledger.new_session(duration_ms=400)
op = ledger.reserve(principal='host', request_key='leased-kill', tool_name='checked_add', target_id=None,
    resource_version=None, arguments={'left': 2, 'right': 3}, requires_approval=False)
ledger.start(op.operation_id, token=session.token, duration_ms=400)
print(op.operation_id, flush=True)
time.sleep(60)
"""


def test_killed_lease_owner_is_quarantined_and_never_redispatched(tmp_path):
    path = tmp_path / "leases.sqlite3"
    child = spawn(LEASE_CHILD, path)
    try:
        operation_id = child.stdout.readline().strip()
        assert operation_id
    finally:
        kill(child)
    ledger = LeasedLedger(path)
    bridge = LeasedToolBridge(ledger, principal="host", authority=LocalAuthority.calculator_only())
    arguments = {"request_key": "leased-kill", "left": 2, "right": 3}
    # While the dead owner's lease is still live, nobody else may take the operation.
    assert ledger.recover_expired() == 0
    assert bridge.call(RECORDED_TOOL_NAME, arguments).error_code == "IN_PROGRESS"
    deadline = time.monotonic() + 5
    while ledger.recover_expired() == 0:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    replay = bridge.call(RECORDED_TOOL_NAME, arguments)
    assert replay.error_code == "OUTCOME_UNKNOWN" and replay.operation_id == operation_id
    assert ledger.get(operation_id).state == "unknown"
    assert ledger.history(operation_id).count("leased_dispatch") == 1


WRITER_CHILD = """
import sys
from verified_execution.ledger import OperationLedger
ledger = OperationLedger(sys.argv[1])
print('started', flush=True)
for index in range(100000):
    op = ledger.reserve(principal='host', request_key=f'k{index}', tool_name='checked_add', target_id=None,
        resource_version=None, arguments={'left': index, 'right': 1}, requires_approval=False)
    ledger.begin_dispatch(op.operation_id)
    if index % 3:
        ledger.complete_calculation(op.operation_id, index + 1)
"""


@pytest.mark.parametrize("delay", [0.05, 0.2, 0.5])
def test_killed_mid_transaction_stream_leaves_a_consistent_ledger(tmp_path, delay):
    path = tmp_path / "stream.sqlite3"
    child = spawn(WRITER_CHILD, path)
    try:
        assert child.stdout.readline().strip() == "started"
        time.sleep(delay)
    finally:
        kill(child)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        dispatched = connection.execute("SELECT count(*) FROM operations WHERE state='dispatched'").fetchone()[0]
    ledger = OperationLedger(path)
    assert ledger.recover_interrupted() == dispatched
    with sqlite3.connect(path) as connection:
        rows = connection.execute("""
            SELECT operations.operation_id, state, request_key, result FROM operations
            LEFT JOIN calculation_results USING(operation_id)""").fetchall()
    assert rows, "the child wrote nothing before it was killed"
    for operation_id, state, key, result in rows:
        index = int(key[1:])
        history = ledger.history(operation_id)
        # Each transaction is all or nothing: state, result row and events agree.
        if state == "succeeded":
            assert result == index + 1 and history == ["reserved", "dispatched", "definitive_succeeded"]
        elif state == "unknown":
            assert result is None and history == ["reserved", "dispatched", "recovered_as_unknown"]
        else:
            assert (state, result, history) == ("prepared", None, ["reserved"])
        # Recovery never turns an interrupted operation into a success it did not record.
        expected = index + 1 if state == "succeeded" else None
        assert ledger.read_calculation(operation_id, principal="host") == expected
