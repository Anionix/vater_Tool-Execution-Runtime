#!/usr/bin/env python3
"""task.testing.025: time, resources and cost of each operation.

Each operation is measured in its own child process (so peak memory belongs
to that operation alone) against the source tree given by --source:

  runtime_execute   checked_add through ToolRuntime.execute
  runtime_acall     the same through the async entry (worker thread)
  mcp_call          official MCP SDK client, in-process server
  http_job          HTTP submit and poll until the job finishes
  recorded_legacy   checked_add_recorded through RecordedToolBridge (SQLite)
  recorded_leased   checked_add_recorded through CancellableLeasedToolBridge
  external_create   github_test_create_issue against a local fake destination

Metrics: wall time and CPU time per call (microseconds), peak resident memory
of the child, ledger bytes written per operation, and cost. No paid service
is called by any operation here; cost is counted as requests to an external
service per operation (GitHub's API is rate-limited per request), and paid
API calls, which must stay zero.

  python scripts/measure_operations.py --output evidence/performance/baseline.json
  python scripts/measure_operations.py --output new.json --compare evidence/performance/baseline.json \
      --diff evidence/performance/diff.json

Timings vary by machine; the diff records them with thresholds but only the
deterministic costs (requests, paid calls) are compared exactly by the tests.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ("runtime_execute", "runtime_acall", "mcp_call", "http_job",
              "recorded_legacy", "recorded_leased", "external_create")
THRESHOLDS = {"wall_us.median": 1.5, "cpu_us.median": 1.5, "peak_rss_kib": 1.25,
              "ledger_bytes_per_operation": 1.25}
EXACT = ("external_requests_per_operation", "paid_api_calls_per_operation")
WARMUP = 5


# --- child: measure one operation -------------------------------------------------

def build(operation: str, source: Path, folder: Path, stack):
    """Return (call, counters). call() runs one operation: a function or a coroutine function.
    Async resources are entered on `stack` (an AsyncExitStack) inside the measuring task."""
    from verified_execution.core import LocalAuthority
    counters = {"external_requests": 0}
    grant = LocalAuthority(frozenset({"checked_add", "github_test_create_issue"}))
    sequence = iter(range(10**9))

    if operation == "runtime_execute":
        from verified_execution.runtime import ToolRuntime
        runtime = ToolRuntime(grant)
        return (lambda: runtime.execute("checked_add", {"left": 2, "right": 3})), counters
    if operation == "runtime_acall":
        from verified_execution.runtime import ToolRuntime
        runtime = ToolRuntime(grant)
        return (lambda: runtime.acall("checked_add", {"left": 2, "right": 3})), counters
    if operation == "mcp_call":
        from importlib import util
        from mcp import Client
        spec = util.spec_from_file_location("measured_server_mcp", source / "server_mcp.py")
        module = util.module_from_spec(spec)
        spec.loader.exec_module(module)

        async def call():
            if "client" not in counters:
                counters["client"] = await stack.enter_async_context(
                    Client(module.build_server(grant), mode="2026-07-28"))
            return await counters["client"].call_tool("checked_add", {"left": 2, "right": 3})
        return call, counters
    if operation == "http_job":
        import asyncio
        from verified_execution.http_local import create_app
        token = "measureOnlyCredential" * 2
        app = create_app(token, grant)

        async def exchange(method, path, body=b""):
            sent, messages = [], [{"type": "http.request", "body": body, "more_body": False}]

            async def receive():
                return messages.pop(0) if messages else {"type": "http.disconnect"}

            async def send(message):
                sent.append(message)
            await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                       "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
                       "root_path": "", "server": ("127.0.0.1", 80), "client": ("127.0.0.1", 1),
                       "headers": [(b"authorization", f"Bearer {token}".encode()),
                                   (b"content-type", b"application/json")]}, receive, send)
            return json.loads(b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body"))

        async def job():
            started = await exchange("POST", "/jobs", b'{"left": 2, "right": 3}')
            while True:
                state = await exchange("GET", f"/jobs/{started['job_id']}")
                if state["state"] not in ("prepared", "running"):
                    return state
                await asyncio.sleep(0.0005)  # a client polling every half millisecond
        return job, counters
    if operation in ("recorded_legacy", "recorded_leased"):
        def arguments():
            return {"request_key": f"k{next(sequence)}", "left": 2, "right": 3}
        if operation == "recorded_legacy":
            from verified_execution.ledger import OperationLedger
            from verified_execution.recorded_tool_bridge import RecordedToolBridge
            bridge = RecordedToolBridge(OperationLedger(folder / "ledger.sqlite3"), principal="host", authority=grant)
            return (lambda: bridge.call("checked_add_recorded", arguments())), counters
        from verified_execution.cancellable_bridge import CancellableLeasedToolBridge
        from verified_execution.worker_leases import LeasedLedger
        bridge = CancellableLeasedToolBridge(LeasedLedger(folder / "ledger.sqlite3"), principal="host", authority=grant)
        return (lambda: bridge.acall("checked_add_recorded", arguments())), counters
    if operation == "external_create":
        from verified_execution.external_change import (
            ExternalChangeExecutor, ExternalResultStore, GitHubIssueDestination, HttpReply)
        from verified_execution.ledger import OperationLedger

        class LocalDestination:
            """A local stand-in for the GitHub API: no network, every request counted."""

            def __init__(self):
                self.issues = []

            def request(self, method, path, *, json_body=None, query=None):
                counters["external_requests"] += 1
                if method == "POST":
                    issue = {"number": len(self.issues) + 1, "node_id": "I", "title": json_body["title"],
                             "body": json_body["body"], "labels": [{"name": n} for n in json_body["labels"]],
                             "updated_at": "2026-11-27T00:00:00Z"}
                    self.issues.append(issue)
                    return HttpReply(201, {}, issue)
                if path.startswith("/issues/"):
                    return HttpReply(200, {"etag": '"e"'}, self.issues[int(path.rsplit("/", 1)[1]) - 1])
                return HttpReply(200, {}, [])

        ledger = OperationLedger(folder / "ledger.sqlite3")

        def approve(op, approved_arguments):
            approval_id = f"approval-{next(sequence)}"
            ledger.issue_approval(approval_id=approval_id, principal=op.principal, tool_name=op.tool_name,
                                  target_id=op.target_id, resource_version=op.resource_version,
                                  arguments=approved_arguments, expires_at_ms=int(time.time() * 1000) + 60_000)
            return approval_id
        executor = ExternalChangeExecutor(ledger, ExternalResultStore(folder / "ledger.sqlite3"),
                                          GitHubIssueDestination(LocalDestination()), principal="host",
                                          authority=grant, target_id="example/repository", approve=approve)
        return (lambda: executor.call("github_test_create_issue",
                                      {"request_key": f"k{next(sequence)}", "title": "Measured issue"})), counters
    raise SystemExit(f"unknown operation {operation}")


def child(operation: str, iterations: int, source: Path) -> dict:
    import asyncio
    import inspect
    from contextlib import AsyncExitStack
    sys.path[:0] = [str(source / "src")]

    async def measure(folder: Path):
        async with AsyncExitStack() as stack:
            call, counters = build(operation, source, folder, stack)

            async def once():
                result = call()
                if inspect.isawaitable(result):
                    await result
            for _ in range(WARMUP):
                await once()
            before = counters["external_requests"]
            size_before = sum(path.stat().st_size for path in folder.glob("*"))
            walls, cpus = [], []
            for _ in range(iterations):
                wall, cpu = time.perf_counter_ns(), time.process_time_ns()
                await once()
                cpus.append((time.process_time_ns() - cpu) / 1000)
                walls.append((time.perf_counter_ns() - wall) / 1000)
            requests = counters["external_requests"] - before
            written = sum(path.stat().st_size for path in folder.glob("*")) - size_before
        return walls, cpus, requests, written

    with tempfile.TemporaryDirectory(prefix="vater-measure-") as directory:
        walls, cpus, requests, written = asyncio.run(measure(Path(directory)))
    walls.sort()
    return {
        "wall_us": {"median": round(statistics.median(walls), 1),
                    "p95": round(walls[max(0, int(len(walls) * 0.95) - 1)], 1), "max": round(walls[-1], 1)},
        "cpu_us": {"median": round(statistics.median(cpus), 1)},
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "ledger_bytes_per_operation": round(max(written, 0) / iterations, 1),
        "external_requests_per_operation": requests / iterations,
        "paid_api_calls_per_operation": 0,
    }


# --- parent: run every operation, record, compare -------------------------------------

def run_operation(operation: str, iterations: int, source: Path) -> dict:
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--child", operation, "--iterations", str(iterations),
         "--source", str(source)], capture_output=True, text=True, timeout=600, check=False,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"})
    if completed.returncode != 0:
        raise RuntimeError(f"{operation} failed: {completed.stderr[-2000:]}")
    return json.loads(completed.stdout)


def measure_all(iterations: int, source: Path, label: str, operations=OPERATIONS) -> dict:
    try:
        commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    return {
        "kind": "operation_costs",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"commit": commit, "label": label},
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "machine": platform.machine(), "cpu_count": os.cpu_count()},
        "iterations": iterations,
        "cost_model": "No paid service is called. Cost is requests to an external service per operation; "
                      "paid API calls must stay zero.",
        "operations": {operation: run_operation(operation, iterations, source) for operation in operations},
    }


def metric(values: dict, name: str):
    for part in name.split("."):
        values = values[part]
    return values


def compare(baseline: dict, candidate: dict) -> dict:
    """Changes per metric, and the ones beyond the thresholds or any change in an exact cost."""
    changes, regressions = {}, []
    for operation, measured in candidate["operations"].items():
        base = baseline["operations"].get(operation)
        if base is None:
            regressions.append({"operation": operation, "metric": "*", "reason": "no baseline"})
            continue
        changes[operation] = {}
        for name in (*THRESHOLDS, *EXACT):
            old, new = metric(base, name), metric(measured, name)
            ratio = None if not old else round(new / old, 3)
            changes[operation][name] = {"baseline": old, "candidate": new, "ratio": ratio}
            if name in EXACT:
                if new != old:
                    regressions.append({"operation": operation, "metric": name, "reason": f"{old} -> {new}"})
            elif ratio is not None and ratio > THRESHOLDS[name]:
                regressions.append({"operation": operation, "metric": name,
                                    "reason": f"{old} -> {new} exceeds x{THRESHOLDS[name]}"})
    return {"thresholds": THRESHOLDS, "exact": list(EXACT), "changes": changes, "regressions": regressions}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", choices=OPERATIONS)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--label", default="working tree")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--diff", type=Path)
    args = parser.parse_args()
    if args.child:
        print(json.dumps(child(args.child, args.iterations, args.source.resolve())))
        return 0
    record = measure_all(args.iterations, args.source.resolve(), args.label)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if args.compare:
        raw = args.compare.read_bytes()
        diff = {"baseline": str(args.compare), "baseline_sha256": hashlib.sha256(raw).hexdigest(),
                "baseline_source": json.loads(raw)["source"], "candidate_source": record["source"],
                **compare(json.loads(raw), record)}
        if args.diff:
            args.diff.parent.mkdir(parents=True, exist_ok=True)
            args.diff.write_text(json.dumps(diff, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(diff["regressions"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
