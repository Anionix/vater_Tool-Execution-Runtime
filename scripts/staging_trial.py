#!/usr/bin/env python3
"""task.operations.010: a small staging trial that never touches production data.

  python scripts/staging_trial.py [--output staging-trial.json] [--keep DIR]

Everything runs inside one new staging directory with synthetic data only:
the real MCP server over stdio (official SDK client) with a staging ledger,
the external-change tool against an in-memory synthetic destination (no
network), the kill switch, a backup and restore of the staging ledger, and the
operation monitor. The server child gets a fresh environment (never a copy of
os.environ), so no credential reaches it; names of credentials that were
present are reported as withheld. The trial refuses to start when the host
points VERIFIED_EXECUTION_LEDGER_PATH at a ledger outside the staging
directory, since that is real data. Exit status 0 only if every check passed.
"""
from __future__ import annotations

import argparse
from importlib import util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from verified_execution.core import LocalAuthority  # noqa: E402
from verified_execution.external_change import (  # noqa: E402
    TOOL_NAME, ExternalChangeExecutor, ExternalResultStore, GitHubIssueDestination, HttpReply)
from verified_execution.kill_switch import KillSwitch  # noqa: E402
from verified_execution.ledger import OperationLedger  # noqa: E402

CREDENTIAL_NAMES = ("GITHUB_TOKEN", "GH_TOKEN", "VATER_LOCAL_HTTP_TOKEN", "AWS_ACCESS_KEY_ID",
                    "AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
LEDGER_VARIABLE = "VERIFIED_EXECUTION_LEDGER_PATH"


def load_script(name: str):
    spec = util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class SyntheticIssues:
    """In-memory stand-in for the issue destination: synthetic data, no network."""

    def __init__(self):
        self.issues, self.posts = [], 0

    def request(self, method, path, *, json_body=None, query=None):
        if method == "POST" and path == "/issues":
            self.posts += 1
            issue = {"number": len(self.issues) + 1, "node_id": f"S_{len(self.issues) + 1}",
                     "title": json_body["title"], "body": json_body["body"],
                     "labels": [{"name": name} for name in json_body["labels"]],
                     "updated_at": "2026-11-27T00:00:00Z"}
            self.issues.append(issue)
            return HttpReply(201, {}, issue)
        if method == "GET" and path == "/issues":
            page = query["page"]
            visible = [i for i in self.issues if query["labels"] in {label["name"] for label in i["labels"]}]
            return HttpReply(200, {}, visible[(page - 1) * 100: page * 100])
        if method == "GET" and path.startswith("/issues/"):
            issue = self.issues[int(path.rsplit("/", 1)[1]) - 1]
            return HttpReply(200, {"etag": f'W/"staging-{issue["number"]}"'}, issue)
        return HttpReply(404, {}, None)


def preflight(staging: Path, environ=os.environ) -> list[str]:
    configured = environ.get(LEDGER_VARIABLE)
    if configured and not Path(configured).resolve().is_relative_to(staging.resolve()):
        return [f"{LEDGER_VARIABLE} points outside the staging directory; refusing to use real data"]
    return []


def mcp_workload(staging: Path) -> dict:
    import anyio
    from mcp import Client, StdioServerParameters
    from mcp.client.stdio import stdio_client

    ledger = staging / "mcp-ledger.sqlite3"
    diagnostics = staging / "server-diagnostics.jsonl"
    # A fresh environment: nothing from the host's environment, so no credential, reaches the server.
    env = {"PYTHONPATH": str(ROOT / "src"), LEDGER_VARIABLE: str(ledger), "HOME": str(staging / "home")}
    observed = {}

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-u", str(ROOT / "server_mcp.py")],
                                       cwd=str(staging), env=env)
        # The server's diagnostics go to a staging file, never to whatever sys.stderr was when
        # the SDK was imported (its stdio_client binds that default once).
        with diagnostics.open("w", encoding="utf-8") as errlog:
            async with Client(stdio_client(params, errlog=errlog), mode="2026-07-28") as client:
                await calls(client)

    async def calls(client):
        observed["tools"] = sorted(tool.name for tool in (await client.list_tools()).tools)
        added = await client.call_tool("checked_add", {"left": 40, "right": 2})
        observed["checked_add"] = added.structured_content
        overflow = await client.call_tool("checked_add", {"left": 4294967295, "right": 1})
        observed["overflow_is_error"] = overflow.is_error
        first = await client.call_tool("checked_add_recorded", {"request_key": "staging-1", "left": 1, "right": 2})
        again = await client.call_tool("checked_add_recorded", {"request_key": "staging-1", "left": 1, "right": 2})
        observed["recorded"] = first.structured_content
        observed["replayed"] = again.structured_content
        denied = await client.call_tool("delete_file", {"path": "/"})
        observed["unregistered_is_error"] = denied.is_error

    anyio.run(run)
    observed["history"] = OperationLedger(ledger).history(observed["recorded"]["operation_id"])
    monitoring = load_script("monitor_operations")
    monitor = monitoring.OperationMonitor(monitoring.Rules.load())
    monitoring.feed_diagnostics(monitor, diagnostics.read_text(encoding="utf-8").splitlines(), clock=lambda: 0)
    observed["diagnostics"] = monitor.report()["windows"][-1]
    return observed


def external_workload(staging: Path) -> dict:
    ledger_path = staging / "external-ledger.sqlite3"
    destination_store = SyntheticIssues()
    switch = KillSwitch(staging / "kill-switch")
    ledger = OperationLedger(ledger_path)
    count = {"n": 0}

    def approve(op, arguments):
        count["n"] += 1
        approval_id = f"staging-approval-{count['n']}"
        ledger.issue_approval(approval_id=approval_id, principal=op.principal, tool_name=op.tool_name,
                              target_id=op.target_id, resource_version=op.resource_version,
                              arguments=arguments, expires_at_ms=int(time.time() * 1000) + 60_000)
        return approval_id

    executor = ExternalChangeExecutor(
        ledger, ExternalResultStore(ledger_path), GitHubIssueDestination(destination_store),
        principal="staging-operator", authority=LocalAuthority(frozenset({TOOL_NAME}), revocation_event=switch),
        target_id="staging/synthetic", approve=approve)
    created = executor.call(TOOL_NAME, {"request_key": "staging-issue-1", "title": "Synthetic staging issue"})
    replay = executor.call(TOOL_NAME, {"request_key": "staging-issue-1", "title": "Synthetic staging issue"})
    switch.engage("staging trial")
    stopped = executor.call(TOOL_NAME, {"request_key": "staging-issue-2", "title": "Never sent"})

    backups = load_script("backup_ledger")
    backup = staging / "external-ledger.backup.sqlite3"
    backups.backup(ledger_path, backup)
    restored = staging / "restored-ledger.sqlite3"
    backups.restore(backup, restored)
    after_restore = ExternalChangeExecutor(
        OperationLedger(restored), ExternalResultStore(restored), GitHubIssueDestination(destination_store),
        principal="staging-operator", authority=LocalAuthority(frozenset({TOOL_NAME})),
        target_id="staging/synthetic", approve=None).call(
        TOOL_NAME, {"request_key": "staging-issue-1", "title": "Synthetic staging issue"})

    monitoring = load_script("monitor_operations")
    report = monitoring.monitor_ledger(ledger_path, monitoring.Rules.load()).report()
    return {"created": created.ok, "replayed": replay.replayed and replay.value == created.value,
            "stopped_by_kill_switch": stopped.error_code, "posts": destination_store.posts,
            "replayed_after_restore": after_restore.ok and after_restore.value == created.value,
            "monitor_alerts": report["alerts"]}


def staged_files(staging: Path) -> list[str]:
    return sorted(str(path.relative_to(staging)) for path in staging.rglob("*") if path.is_file())


def run(staging: Path, environ=os.environ) -> dict:
    report = {"staging_dir": str(staging), "data": "synthetic only",
              "credentials_withheld": sorted(name for name in CREDENTIAL_NAMES if environ.get(name))}
    refused = preflight(staging, environ)
    if refused:
        return {**report, "refused": refused, "checks": {}, "passed": False}
    (staging / "home").mkdir(parents=True, exist_ok=True)
    mcp = mcp_workload(staging)
    external = external_workload(staging)
    with sqlite3.connect(staging / "mcp-ledger.sqlite3") as connection:
        principals = sorted(row[0] for row in connection.execute("SELECT DISTINCT principal FROM operations"))
    checks = {
        "mcp_tools_listed": mcp["tools"] == ["checked_add", "checked_add_recorded"],
        "mcp_checked_add": mcp["checked_add"] == {"result": 42},
        "mcp_refuses_overflow": mcp["overflow_is_error"],
        "mcp_refuses_unregistered_tool": mcp["unregistered_is_error"],
        "mcp_recorded_replay_without_dispatch": mcp["replayed"].get("replayed") is True
        and mcp["history"] == ["reserved", "leased_dispatch", "leased_completed"],
        "external_created_once": external["created"] and external["replayed"] and external["posts"] == 1,
        "kill_switch_stops": external["stopped_by_kill_switch"] == "NOT_AUTHORIZED",
        "restore_replays_without_resend": external["replayed_after_restore"] and external["posts"] == 1,
        "monitor_quiet": external["monitor_alerts"] == [],
        "diagnostics_monitored": (mcp["diagnostics"]["operations"], mcp["diagnostics"]["errors"]) == (5, 2),
        "only_staging_principals": principals == ["local-process"],
    }
    return {**report, "mcp": mcp, "external": external, "files": staged_files(staging), "checks": checks,
            "passed": all(checks.values())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep", type=Path, help="use (and keep) this new staging directory")
    args = parser.parse_args(argv)
    if args.keep is not None:
        args.keep.mkdir(parents=True, exist_ok=False)
        report = run(args.keep)
    else:
        with tempfile.TemporaryDirectory(prefix="vater-staging-") as directory:
            report = run(Path(directory))
    text = json.dumps(report, indent=1, default=str)
    if args.output is not None:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
