"""task.operations.011: operation counts and the error rate are monitored and abnormal increases detected.

scripts/monitor_operations.py counts operations and errors per window, from
the durable ledger (read-only) or from the MCP server's diagnostics, and
compares the latest window with its baseline. These tests require steady
traffic to stay quiet, and an operation burst, an error burst and a high
error rate each to raise their alert, from both sources and from the CLI.
"""
from __future__ import annotations

from hashlib import sha256
from importlib import util
import json
from pathlib import Path
import sys

import pytest

from adapter_drivers import load_mcp_adapter, with_sdk_client
from verified_execution.ledger import OperationLedger

ROOT = Path(__file__).resolve().parents[1]
spec = util.spec_from_file_location("monitor_operations", ROOT / "scripts" / "monitor_operations.py")
monitoring = util.module_from_spec(spec)
sys.modules[spec.name] = monitoring  # dataclasses resolve annotations through it
spec.loader.exec_module(monitoring)
RULES = monitoring.Rules.load()
MINUTE = RULES.window_ms
T0 = 1_800_000_000_000 - 1_800_000_000_000 % MINUTE


def steady(monitor, windows=10, per_window=10, errors=0):
    for window in range(windows):
        for n in range(per_window):
            monitor.observe(T0 + window * MINUTE + n, error=n < errors)


def rules_of(alerts):
    return sorted(alert["rule"] for alert in alerts)


def test_the_rules_are_the_configured_ones():
    assert RULES == monitoring.Rules(window_ms=60_000, baseline_windows=10, spike_factor=3.0,
                                     min_spike_operations=20, min_spike_errors=5,
                                     error_rate_threshold=0.2, min_rate_operations=10)


def test_steady_traffic_with_rare_errors_raises_nothing():
    monitor = monitoring.OperationMonitor(RULES)
    steady(monitor, windows=11, per_window=12, errors=1)
    assert monitor.check() == []
    assert monitor.report()["windows"][-1] == {"start_ms": T0 + 10 * MINUTE, "operations": 12, "errors": 1,
                                               "denied": 0, "error_rate": 0.0833}


def test_an_operation_burst_is_detected():
    monitor = monitoring.OperationMonitor(RULES)
    steady(monitor)
    for n in range(31):  # 31 > max(20, 3 x 10)
        monitor.observe(T0 + 10 * MINUTE + n)
    assert rules_of(monitor.check()) == ["operation_spike"]
    assert monitor.check()[0]["limit"] == 30.0


def test_an_error_burst_and_a_high_error_rate_are_detected():
    monitor = monitoring.OperationMonitor(RULES)
    steady(monitor)
    for n in range(12):
        monitor.observe(T0 + 10 * MINUTE + n, error=n < 6)
    alerts = monitor.check()
    assert rules_of(alerts) == ["error_rate", "error_spike"]
    assert {a["rule"]: a["value"] for a in alerts} == {"error_rate": 0.5, "error_spike": 6}


def test_too_few_operations_do_not_raise_a_rate_alert_and_quiet_windows_count_as_baseline():
    monitor = monitoring.OperationMonitor(RULES)
    monitor.observe(T0, error=True)
    assert monitor.check() == []  # 1 of 1 failed, but below min_rate_operations
    monitor.observe(T0 + 5 * MINUTE)
    assert [w["operations"] for w in monitor.report()["windows"]] == [1, 0, 0, 0, 0, 1]


def ledger_with_traffic(path: Path, monkeypatch, burst_failures: int) -> None:
    ledger = OperationLedger(path)
    clock = {"now": T0}
    monkeypatch.setattr(OperationLedger, "_now", staticmethod(lambda: clock["now"]))
    count = 0

    def operation(at_ms, fail=False):
        nonlocal count
        count += 1
        clock["now"] = at_ms
        op = ledger.reserve(principal="p", request_key=f"k-{count}", tool_name="checked_add_recorded",
                            target_id=None, resource_version=None, arguments={"n": count},
                            requires_approval=False)
        ledger.begin_dispatch(op.operation_id)
        ledger.record_definitive_result(op.operation_id, "failed" if fail else "succeeded")

    for window in range(10):
        for n in range(5):
            operation(T0 + window * MINUTE + n * 1000)
    for n in range(12):
        operation(T0 + 10 * MINUTE + n * 1000, fail=n < burst_failures)


def test_the_ledger_source_detects_failures_and_never_writes(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite3"
    ledger_with_traffic(path, monkeypatch, burst_failures=7)
    before = sha256(path.read_bytes()).hexdigest()
    report = monitoring.monitor_ledger(path, RULES, now_ms=T0 + 10 * MINUTE + 30_000).report()
    assert [w["operations"] for w in report["windows"]] == [5] * 10 + [12]
    assert report["windows"][-1]["errors"] == 7
    assert rules_of(report["alerts"]) == ["error_rate", "error_spike"]
    assert sha256(path.read_bytes()).hexdigest() == before
    with pytest.raises(Exception):
        monitoring.ledger_events(tmp_path / "missing.sqlite3")  # never creates a ledger
    assert not (tmp_path / "missing.sqlite3").exists()


def test_the_ledger_source_stays_quiet_for_normal_traffic(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite3"
    ledger_with_traffic(path, monkeypatch, burst_failures=0)
    assert monitoring.monitor_ledger(path, RULES, now_ms=T0 + 10 * MINUTE + 30_000).check() == []


def test_the_real_server_diagnostics_feed_the_monitor(capfd):
    pytest.importorskip("mcp")
    server = load_mcp_adapter("server_mcp_monitoring").build_server(call_budget=2)

    async def scenario(client):
        await client.call_tool("checked_add", {"left": 1, "right": 2})
        await client.call_tool("checked_add", {"left": 1, "right": -2})
        await client.call_tool("checked_add", {"left": 1, "right": 2})  # over budget: denied

    with_sdk_client(server, scenario)
    lines = capfd.readouterr().err.splitlines()
    monitor = monitoring.OperationMonitor(RULES)
    monitoring.feed_diagnostics(monitor, lines, clock=lambda: T0)
    assert monitor.report()["windows"] == [{"start_ms": T0, "operations": 3, "errors": 2, "denied": 1,
                                            "error_rate": 0.6667}]


def test_the_cli_reports_each_alert_once_and_exits_two(tmp_path, capsys):
    quiet = tmp_path / "quiet.jsonl"
    quiet.write_text('{"source":"mcp.tools.call","outcome":"succeeded"}\nnot json\n{"source":"other"}\n')
    assert monitoring.main(["--diagnostics", str(quiet)]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["alerts"] == []
    noisy = tmp_path / "noisy.jsonl"
    noisy.write_text('{"source":"mcp.tools.call","outcome":"rejected"}\n' * 30)
    assert monitoring.main(["--diagnostics", str(noisy)]) == 2
    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    alerts = [line["alert"]["rule"] for line in printed if "alert" in line]
    assert sorted(alerts) == ["error_rate", "error_spike", "operation_spike"]  # once each, not per line


def test_the_cli_reads_a_ledger(tmp_path, capsys):
    path = tmp_path / "ledger.sqlite3"
    OperationLedger(path)
    assert monitoring.main(["--ledger", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["alerts"] == []
