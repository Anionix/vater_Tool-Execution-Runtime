"""task.testing.020: state transitions under concurrency, checked by counterexample.

Each ledger method is one SQLite transaction (BEGIN IMMEDIATE), so concurrent
callers interleave at transaction granularity. explore() runs EVERY order of
the actors' transactions, each order on a fresh database with a controlled
clock, and checks the invariants after it. Three races are covered
independently: reservation of one request key, cancellation against dispatch
and result, and an ownership switch when a lease expires.

A search that never fails proves little, so each race is also run against a
deliberately broken ledger (one guard removed): explore() must find at least
one counterexample there. Orders inside a single transaction are not
enumerated; docs/concurrency_inventory.json records that as unchecked.

The last tests close three concurrent paths that had no test (inventory
entries S9, T2 and A2) with real threads or real task cancellation.
"""
from __future__ import annotations

import asyncio
from itertools import permutations
import sqlite3
import threading
import time

from test_external_change import GRANT, REPO, FakeGitHub, Host
from verified_execution.external_change import (
    TOOL_NAME, ExternalChangeExecutor, ExternalResultStore, GitHubIssueDestination)
from verified_execution.ledger import LedgerError, OperationLedger
from verified_execution.model_mock import ModelContractError, ModelMessage, ModelRequest, OfflineResponses
from verified_execution.worker_leases import LeasedLedger

ARGS = {"left": 2, "right": 3}
LEASE_MS = 1_000


def schedules(*lengths):
    """Every interleaving of actors with these step counts, as actor indexes."""
    labels = [actor for actor, count in enumerate(lengths) for _ in range(count)]
    return sorted(set(permutations(labels)))


def explore(tmp_path, make_ledger, actors, invariant, setup=None):
    """Run each schedule on a fresh ledger; return (order, problem, log) for each failure."""
    failures = []
    for index, order in enumerate(schedules(*(len(steps) for steps in actors))):
        world = {"ledger": make_ledger(tmp_path / f"schedule-{index}.sqlite3"), "clock": [0], "log": [], "ops": {}}
        world["ledger"]._now = lambda world=world: world["clock"][0]
        if setup is not None:
            setup(world)
        cursors = [0] * len(actors)
        for actor in order:
            step = actors[actor][cursors[actor]]
            cursors[actor] += 1
            try:
                step(world)
                result = "ok"
            except LedgerError as error:
                result = error.code
            except sqlite3.Error as error:  # the bridges report these as DATABASE_ERROR
                result = type(error).__name__
            world["log"].append((actor, step.__name__, result, world["clock"][0]))
        problem = invariant(world)
        if problem:
            failures.append((order, problem, world["log"]))
    return failures


# --- 1. reservation of one request key ------------------------------------------------

def reserving(name, arguments=ARGS):
    def reserve(world):
        op = world["ledger"].reserve(principal="host", request_key="shared", tool_name="checked_add",
                                     target_id=None, resource_version=None, arguments=arguments,
                                     requires_approval=False)
        world["ops"][name] = op.operation_id

    def dispatch(world):
        world["ledger"].begin_dispatch(world["ops"][name])

    def complete(world):
        world["ledger"].complete_calculation(world["ops"][name], 5)
    return [reserve, dispatch, complete]


def one_dispatch(world):
    ledger, ops = world["ledger"], world["ops"]
    if len(set(ops.values())) != 1:
        return f"two operations for one key: {ops}"
    (operation_id,) = set(ops.values())
    history = ledger.history(operation_id)
    if history.count("dispatched") != 1 or history.count("definitive_succeeded") != 1:
        return f"history {history}"
    if ledger.read_calculation(operation_id, principal="host") != 5:
        return "result missing"


def test_every_order_of_two_reservations_dispatches_once(tmp_path):
    assert len(schedules(3, 3)) == 20
    assert explore(tmp_path, OperationLedger, [reserving("a"), reserving("b")], one_dispatch) == []


def test_conflicting_reservations_keep_the_first_intent_in_every_order(tmp_path):
    def first_wins(world):
        outcomes = {actor: result for actor, step, result, _ in world["log"] if step == "reserve"}
        if sorted(outcomes.values()) != ["IDEMPOTENCY_CONFLICT", "ok"]:
            return f"reservations {outcomes}"
        first = next(actor for actor, step, _, _ in world["log"] if step == "reserve")
        if outcomes[first] != "ok":
            return "a later reservation replaced the first intent"

    actors = [reserving("a")[:1], reserving("b", {"left": 9, "right": 9})[:1]]
    assert explore(tmp_path, OperationLedger, actors, first_wins) == []


class DoubleDispatchLedger(OperationLedger):
    """Broken on purpose: begin_dispatch without the prepared-state guard."""

    def begin_dispatch(self, operation_id, *, approval_id=None, authority=None):
        with self._transaction() as connection:
            connection.execute("UPDATE operations SET state='dispatched' WHERE operation_id=?", (operation_id,))
            self._event(connection, operation_id, "dispatched", self._now())


def test_reservation_search_finds_the_counterexample_in_a_broken_ledger(tmp_path):
    failures = explore(tmp_path, DoubleDispatchLedger, [reserving("a"), reserving("b")], one_dispatch)
    assert failures and all(problem.startswith("history") for _, problem, _ in failures)


# --- 2. cancellation against dispatch and result ----------------------------------------

def cancel(world):
    world["ledger"].request_cancel(world["ops"]["a"])


def cancel_is_never_an_undo(world):
    ledger, operation_id = world["ledger"], world["ops"]["a"]
    op, history = ledger.get(operation_id), ledger.history(operation_id)
    value = ledger.read_calculation(operation_id, principal="host")
    if op.state == "cancelled":
        if "dispatched" in history or value is not None:
            return f"cancelled after dispatch: {history}"
    elif "dispatched" in history:
        # A cancel that arrives after dispatch is a flag, never a rollback.
        if op.state != "succeeded" or value != 5:
            return f"dispatched operation ended {op.state} with {value}"
    else:
        return f"neither cancelled nor dispatched: {op.state} {history}"


def test_every_order_of_cancel_and_completion_keeps_its_meaning(tmp_path):
    reserve, dispatch, complete = reserving("a")
    failures = explore(tmp_path, OperationLedger, [[dispatch, complete], [cancel]], cancel_is_never_an_undo,
                       setup=reserve)
    assert failures == []


class UndoingCancelLedger(OperationLedger):
    """Broken on purpose: cancel rewrites any state, even after dispatch."""

    def request_cancel(self, operation_id):
        with self._transaction() as connection:
            connection.execute("UPDATE operations SET state='cancelled' WHERE operation_id=?", (operation_id,))
            self._event(connection, operation_id, "cancelled_before_dispatch", self._now())
        return self.get(operation_id)


def test_cancellation_search_finds_the_counterexample_in_a_broken_ledger(tmp_path):
    reserve, dispatch, complete = reserving("a")
    failures = explore(tmp_path, UndoingCancelLedger, [[dispatch, complete], [cancel]], cancel_is_never_an_undo,
                       setup=reserve)
    assert failures and all("after dispatch" in problem for _, problem, _ in failures)


# --- 3. ownership switch when a lease expires ------------------------------------------

def start(world):
    ledger = world["ledger"]
    session = ledger.new_session(duration_ms=LEASE_MS)
    world["token"] = session.token
    ledger.start(world["ops"]["a"], token=session.token, duration_ms=LEASE_MS)


def finish(world):
    world["ledger"].finish(world["ops"]["a"], token=world["token"], value=5)


def recover(world):
    world["ledger"].recover_expired()


def recover_again(world):
    world["ledger"].recover_expired()


def advance(world):
    world["clock"][0] += 2 * LEASE_MS


def reserve_leased(world):
    op = world["ledger"].reserve(principal="host", request_key="leased", tool_name="checked_add", target_id=None,
                                 resource_version=None, arguments=ARGS, requires_approval=False)
    world["ops"]["a"] = op.operation_id


def fenced(world):
    ledger, operation_id, log = world["ledger"], world["ops"]["a"], world["log"]
    history = ledger.history(operation_id)
    finished, quarantined = "leased_completed" in history, "expired_owner_outcome_unknown" in history
    started = next(index for index, entry in enumerate(log) if entry[1] == "start")
    expiry = log[started][3] + LEASE_MS
    if finished and quarantined:
        return f"both completed and quarantined: {history}"
    if finished and next(entry[3] for entry in log if entry[1] == "finish") >= expiry:
        return "an owner whose lease had expired recorded a success"
    late_recovery = any(entry[1].startswith("recover") and entry[3] >= expiry for entry in log[started:])
    if not finished and late_recovery and ledger.get(operation_id).state != "unknown":
        return "recovery left an expired dispatch in place"
    if (ledger.read_calculation(operation_id, principal="host") is not None) != finished:
        return "result and state disagree"


def test_every_order_of_owner_recovery_and_expiry_is_fenced(tmp_path):
    actors = [[start, finish], [recover, recover_again], [advance]]
    assert len(schedules(2, 2, 1)) == 30
    assert explore(tmp_path, LeasedLedger, actors, fenced, setup=reserve_leased) == []


class UnfencedLedger(LeasedLedger):
    """Broken on purpose: finish skips the owner and lease check."""

    def _assert_owner(self, conn, operation_id, token, now):
        return None


def test_ownership_search_finds_the_counterexample_in_a_broken_ledger(tmp_path):
    actors = [[start, finish], [recover, recover_again], [advance]]
    failures = explore(tmp_path, UnfencedLedger, actors, fenced, setup=reserve_leased)
    assert failures and all("expired" in problem for _, problem, _ in failures)


# --- real concurrency for paths that had no test --------------------------------------

def blocking_github():
    github, entered, release = FakeGitHub(), threading.Event(), threading.Event()

    def slow_post(_body):
        entered.set()
        assert release.wait(5)
    github.on_post = slow_post
    return github, entered, release


def test_two_threads_with_one_request_key_create_one_issue(tmp_path):
    """Inventory S9: the external executor called twice at once with one key."""
    github, entered, release = blocking_github()
    host = Host(tmp_path / "ledger.sqlite3", github)
    other = ExternalChangeExecutor(host.ledger, ExternalResultStore(host.path), GitHubIssueDestination(github),
                                   principal="local-host", authority=GRANT, target_id=REPO, approve=host.approve)
    results = {}
    first = threading.Thread(target=lambda: results.setdefault("first", host.create(key="same")))
    first.start()
    try:
        assert entered.wait(5)
        results["second"] = other.call(TOOL_NAME, {"request_key": "same", "title": "Evidence issue"})
    finally:
        release.set()
        first.join(5)
    assert results["first"].ok and results["second"].error_code == "IN_PROGRESS"
    assert results["second"].operation_id == results["first"].operation_id
    assert github.posts == 1 and len(github.issues) == 1


def test_model_call_budget_holds_under_concurrent_calls():
    """Inventory T2: the budget lock admits exactly `budget` fixture calls."""
    calls, gate, outcomes = [], threading.Barrier(16), []
    adapter = OfflineResponses(lambda payload: calls.append(1) or {"output": []}, call_budget=5)
    request = ModelRequest((ModelMessage("user", "hello"),))

    def worker():
        gate.wait()
        try:
            adapter.respond(request)
            outcomes.append("ok")
        except ModelContractError:
            outcomes.append("refused")

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert len(calls) == 5 and outcomes.count("ok") == 5 and outcomes.count("refused") == 11


def test_cancelled_external_call_still_records_its_result(tmp_path):
    """Inventory A2: cancelling the caller does not abandon a started external change."""
    import anyio
    import anyio.lowlevel
    github, entered, release = blocking_github()
    host = Host(tmp_path / "ledger.sqlite3", github)

    async def scenario():
        # The caller gives up after 0.2 s; the destination answers only at 0.6 s.
        # abandon_on_cancel=False keeps the worker until the change is recorded,
        # and only then does the caller see its cancellation.
        timer = threading.Timer(0.6, release.set)
        timer.start()
        with anyio.move_on_after(0.2) as scope:
            await host.executor.acall(TOOL_NAME, {"request_key": "cancelled", "title": "Evidence issue"})
            await anyio.lowlevel.checkpoint()  # the caller's next checkpoint delivers the cancellation
            raise AssertionError("the call should have been cancelled")
        assert scope.cancelled_caught and entered.is_set() and release.is_set()

    try:
        asyncio.run(scenario())
    finally:
        release.set()
    deadline = time.monotonic() + 5
    replay = host.create(key="cancelled")
    while replay.error_code == "IN_PROGRESS" and time.monotonic() < deadline:
        time.sleep(0.05)
        replay = host.create(key="cancelled")
    assert replay.ok and replay.replayed and github.posts == 1
    assert host.store.get(replay.operation_id)["status"] == "succeeded"


STALLED_STDERR_CHILD = """
import fcntl, os, sys, time
sys.path[:0] = sys.argv[1:3]
read_end, write_end = os.pipe()
fcntl.fcntl(write_end, fcntl.F_SETFL, os.O_NONBLOCK)
try:
    while True:
        os.write(write_end, b"x" * 65536)
except BlockingIOError:
    pass
fcntl.fcntl(write_end, fcntl.F_SETFL, 0)  # now full AND blocking: any plain write would hang
os.dup2(write_end, 2)
from adapter_drivers import load_mcp_adapter, with_sdk_client
server = load_mcp_adapter("server_mcp_stalled_stderr").build_server()
async def scenario(client):
    return await client.call_tool("checked_add", {"left": 1, "right": 2})
started = time.monotonic()
reply = with_sdk_client(server, scenario)
os.write(1, f"{reply.structured_content['result']} {time.monotonic() - started:.3f}\\n".encode())
"""


def test_a_stalled_diagnostics_reader_does_not_stop_replies():
    """Inventory T7: the per-call diagnostics line is written only when stderr is writable."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run([sys.executable, "-W", "ignore", "-c", STALLED_STDERR_CHILD,
                                str(root / "src"), str(root / "tests")],
                               capture_output=True, text=True, timeout=30, cwd=root,
                               env=dict(os.environ, PYTHONPATH=str(root / "src")))
    assert completed.returncode == 0, completed.stderr
    result, elapsed = completed.stdout.split()
    assert result == "3" and float(elapsed) < 10
