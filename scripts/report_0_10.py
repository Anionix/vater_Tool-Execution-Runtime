"""Independent evidence summary: read executed JUnit XML, never infer passed tests.

State transition REPORT-010: existing XML -> parse -> gate decision -> report.
A report with skipped official SDK tests CANNOT mark the release as completed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import xml.etree.ElementTree as ET

PROJECT = Path(__file__).resolve().parents[1]
EVIDENCE = PROJECT / 'evidence'
SOURCES = ('baseline.xml', 'ledger_and_cancel.xml', 'lifecycle.xml')


def read_source(filename: str) -> dict:
    path = EVIDENCE / filename
    parent = ET.parse(path).getroot()
    element = parent if parent.tag == 'testsuite' else parent.find('testsuite')
    if element is None:
        raise RuntimeError(f'missing testsuite in {filename}')
    counts = {key: int(element.attrib.get(key, '0')) for key in
              ('tests', 'failures', 'errors', 'skipped')}
    counts['passed'] = counts['tests'] - sum(counts[k] for k in ('failures', 'errors', 'skipped'))
    if counts['passed'] < 0:
        raise RuntimeError(f'inconsistent JUnit counts in {filename}')
    counts['source'] = filename
    counts['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    counts['time_seconds'] = float(element.attrib.get('time','0'))
    return counts


def main():
    suites = {name: read_source(name) for name in SOURCES}
    totals = {key: sum(info[key] for info in suites.values()) for key in
              ('tests','passed','failures','errors','skipped')}
    if (totals['tests'],totals['passed'],totals['skipped'],totals['failures'],totals['errors']) != (284,270,14,0,0):
        raise RuntimeError(f'unexpected test counts: {totals}')
    try:
        mcp_version = importlib.metadata.version('mcp')
    except importlib.metadata.PackageNotFoundError:
        mcp_version = None
    report = {
        'report_identifier': 'python-first-mcp-sqlite-cancellation-2026-11-23',
        'version': '0.10.0',
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'runtime_python': platform.python_version(),
        'platform': platform.platform(),
        'suites': suites,
        'totals': totals,
        'sdk': {
            'installed_version': mcp_version,
            'official_sdk_tests_executed': 0 if mcp_version is None else 'check individual tests',
            'official_sdk_tests_skipped': 14,
            'network_level_mcp_cancellation': 'not_verified'
        },
        'actual_tested': [
            'async event loop remains responsive while SQLite write lock blocks a worker',
            'cancellation while worker creates a session',
            'cancellation during reservation worker wait',
            'cancellation during dispatch worker wait',
            'cancellation during result commit worker wait',
            'database busy errors have stable error code',
            'earlier Python core/SQLite/transport/process tests passed'
        ],
        'not_tested': [
            'official MCP Python SDK in-process and stdio integration',
            'MCP wire-level cancelled response suppression',
            'HTTP cancellation and disconnects',
            'external side effects and remote idempotency',
            'Rust implementation, Kani or Lean proofs'
        ],
        'release_status': 'BLOCKED'
    }
    (EVIDENCE / 'verification_0_10.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'totals':totals,'release_status':report['release_status']},ensure_ascii=False))

if __name__ == '__main__':
    main()
