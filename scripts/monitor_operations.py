#!/usr/bin/env python3
"""task.operations.011: monitor operation counts and the error rate, and detect abnormal increases.

  python scripts/monitor_operations.py --ledger var/ledger.sqlite3
  python server_mcp.py 2> >(python scripts/monitor_operations.py --diagnostics -)

Two sources feed the same windowed counter:
  --ledger       the durable ledger's events table, opened read-only
                 ('reserved' is one operation; 'definitive_failed',
                 'outcome_unknown' and 'recovered_as_unknown' are errors);
  --diagnostics  the MCP server's diagnostics lines ("source":"mcp.tools.call"),
                 stamped with the host clock on arrival; any outcome other than
                 'succeeded' is an error, and 'denied' is also counted apart.

Rules (config/monitoring.json) compare the latest window with the mean of the
preceding baseline windows: operation_spike, error_spike and error_rate. Any
alert is printed as JSON and the exit status is 2; a quiet check exits 0.
"""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, field
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "monitoring.json"
LEDGER_ERRORS = ("definitive_failed", "outcome_unknown", "recovered_as_unknown")


@dataclass
class Window:
    start_ms: int
    operations: int = 0
    errors: int = 0
    denied: int = 0

    def as_dict(self) -> dict:
        rate = self.errors / self.operations if self.operations else 0.0
        return {"start_ms": self.start_ms, "operations": self.operations, "errors": self.errors,
                "denied": self.denied, "error_rate": round(rate, 4)}


@dataclass
class Rules:
    window_ms: int = 60_000
    baseline_windows: int = 10
    spike_factor: float = 3.0
    min_spike_operations: int = 20
    min_spike_errors: int = 5
    error_rate_threshold: float = 0.2
    min_rate_operations: int = 10

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG) -> "Rules":
        return cls(**json.loads(path.read_text(encoding="utf-8"))["rules"])


@dataclass
class OperationMonitor:
    rules: Rules = field(default_factory=Rules)
    windows: deque = field(default_factory=deque)

    def window(self, at_ms: int) -> Window:
        start = at_ms - at_ms % self.rules.window_ms
        if not self.windows or self.windows[-1].start_ms < start:
            # Windows without any observation count as quiet baseline windows.
            next_start = self.windows[-1].start_ms + self.rules.window_ms if self.windows else start
            while next_start <= start:
                self.windows.append(Window(next_start))
                next_start += self.rules.window_ms
            while len(self.windows) > self.rules.baseline_windows + 1:
                self.windows.popleft()
        for window in reversed(self.windows):
            if window.start_ms == start:
                return window
        raise ValueError("observation older than the monitored windows")

    def observe(self, at_ms: int, *, error: bool = False, denied: bool = False) -> None:
        window = self.window(at_ms)
        window.operations += 1
        window.errors += bool(error or denied)
        window.denied += bool(denied)

    def check(self) -> list[dict]:
        if not self.windows:
            return []
        current, baseline = self.windows[-1], list(self.windows)[:-1]
        rules, alerts = self.rules, []
        mean_ops = sum(w.operations for w in baseline) / len(baseline) if baseline else 0.0
        mean_errors = sum(w.errors for w in baseline) / len(baseline) if baseline else 0.0
        ops_limit = max(rules.min_spike_operations, rules.spike_factor * mean_ops)
        if current.operations > ops_limit:
            alerts.append({"rule": "operation_spike", "value": current.operations, "limit": ops_limit})
        errors_limit = max(rules.min_spike_errors, rules.spike_factor * mean_errors)
        if current.errors > errors_limit:
            alerts.append({"rule": "error_spike", "value": current.errors, "limit": errors_limit})
        if current.operations >= rules.min_rate_operations:
            rate = current.errors / current.operations
            if rate > rules.error_rate_threshold:
                alerts.append({"rule": "error_rate", "value": round(rate, 4), "limit": rules.error_rate_threshold})
        for alert in alerts:
            alert["window_start_ms"] = current.start_ms
        return alerts

    def report(self) -> dict:
        return {"windows": [w.as_dict() for w in self.windows], "alerts": self.check()}


def ledger_events(path: Path) -> list[tuple[str, int]]:
    # Read-only URI: monitoring never writes to, or creates, the ledger.
    connection = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        return connection.execute(
            "SELECT event_name, event_at_ms FROM events ORDER BY event_at_ms, sequence").fetchall()
    finally:
        connection.close()


def monitor_ledger(path: Path, rules: Rules, now_ms: int | None = None) -> OperationMonitor:
    monitor = OperationMonitor(rules)
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    oldest = now_ms - now_ms % rules.window_ms - rules.baseline_windows * rules.window_ms
    monitor.window(oldest)
    for name, at_ms in ledger_events(path):
        if oldest <= at_ms <= now_ms:
            if name == "reserved":
                monitor.observe(at_ms)
            elif name in LEDGER_ERRORS:
                monitor.window(at_ms).errors += 1
    monitor.window(now_ms)
    return monitor


def diagnostic_outcome(line: str) -> str | None:
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("source") != "mcp.tools.call":
        return None
    outcome = event.get("outcome")
    return outcome if isinstance(outcome, str) else "unknown"


def feed_diagnostics(monitor: OperationMonitor, lines: Iterable[str], clock=lambda: int(time.time() * 1000),
                     emit=None) -> None:
    reported = set()
    for line in lines:
        outcome = diagnostic_outcome(line)
        if outcome is None:
            continue
        monitor.observe(clock(), error=outcome != "succeeded", denied=outcome == "denied")
        if emit is not None:
            for alert in monitor.check():
                key = (alert["rule"], alert["window_start_ms"])
                if key not in reported:  # one alert per rule and window
                    reported.add(key)
                    emit(alert)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--ledger", type=Path)
    source.add_argument("--diagnostics", help="a diagnostics file, or - for standard input")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    rules = Rules.load(args.config)
    if args.ledger is not None:
        report = monitor_ledger(args.ledger, rules).report()
        print(json.dumps(report, indent=1))
        return 2 if report["alerts"] else 0
    monitor, alerts = OperationMonitor(rules), []

    def emit(alert):
        alerts.append(alert)
        print(json.dumps({"alert": alert}), flush=True)

    stream = sys.stdin if args.diagnostics == "-" else open(args.diagnostics, encoding="utf-8")
    with stream:
        feed_diagnostics(monitor, stream, emit=emit)
    print(json.dumps(monitor.report()))
    return 2 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
