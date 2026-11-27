#!/usr/bin/env python3
"""task.operations.007: every test record is bound to the SHA-256 of what it checked; stale evidence is not reused.

  python scripts/evidence_freshness.py                             # classify every evidence record
  python scripts/evidence_freshness.py --require-fresh evidence/gates/testing.json ...
  python scripts/evidence_freshness.py --refresh-gate testing      # rerun live, then rebind

Each task and gate record carries source_sha256 (the files its tests checked)
and, for gates, the SHA-256 of its archived report. A record whose files or
report no longer hash to the recorded values is stale: it says nothing about
the current source. --require-fresh fails on any stale record, and the
release decision and verify_phase --snapshot refuse stale gates; live runs
never read old results. --refresh-gate reruns every case of the phase in a new
process and rebinds the gate record to the current hashes only if that run
passed; a failing run leaves the record untouched.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha1, sha256
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RECORD_DIRECTORIES = ("evidence/verified_tasks", "evidence/gates")


def digest(path: Path) -> str | None:
    return sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def git_blob(path: Path) -> str | None:
    """The git blob id of a file, for records bound by source_blobs."""
    if not path.is_file():
        return None
    data = path.read_bytes()
    return sha1(b"blob %d\0" % len(data) + data).hexdigest()


def freshness(record_path: Path, root: Path = ROOT) -> dict:
    record = json.loads(record_path.read_text(encoding="utf-8"))
    bound, blobs = record.get("source_sha256") or {}, record.get("source_blobs") or {}
    name = str(record_path.relative_to(root)) if record_path.is_relative_to(root) else str(record_path)
    result = {"record": name, "task_id": record.get("task_id")}
    if not bound and not blobs:
        return {**result, "state": "unbound", "changed": []}
    changed = sorted({file for file, expected in bound.items() if digest(root / file) != expected}
                     | {file for file, expected in blobs.items() if git_blob(root / file) != expected})
    if record.get("report_path") and digest(root / record["report_path"]) != record.get("report_sha256"):
        changed.append(record["report_path"])
    return {**result, "state": "stale" if changed else "fresh", "changed": changed}


def records(root: Path = ROOT) -> list[Path]:
    return sorted(path for directory in RECORD_DIRECTORIES for path in (root / directory).glob("*.json"))


def summary(root: Path = ROOT) -> dict:
    rows = [freshness(path, root) for path in records(root)]
    counts = {state: sum(1 for row in rows if row["state"] == state) for state in ("fresh", "stale", "unbound")}
    return {"records": len(rows), **counts, "stale_records": [row for row in rows if row["state"] == "stale"]}


def run_phase(phase: str, report: Path, root: Path = ROOT) -> int:
    return subprocess.run([sys.executable, "scripts/verify_phase.py", phase, f"--junitxml={report}"],
                          cwd=root).returncode


def phase_files(phase: str, root: Path = ROOT) -> set[str]:
    sys.path.insert(0, str(root / "scripts"))
    from verify_phase import phase_cases  # noqa: E402
    return {node.split("::", 1)[0] for cases in phase_cases(phase).values() for node in cases}


def refresh_gate(phase: str, root: Path = ROOT, runner=run_phase, files=phase_files) -> dict:
    path = root / "evidence" / "gates" / f"{phase}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    # A report of its own: several older gates share one report file, which a rerun must not overwrite.
    report_path = f"evidence/gates/{phase}-tests.xml"
    report = root / report_path
    candidate = report.with_name(report.name + ".candidate")
    candidate.unlink(missing_ok=True)
    if runner(phase, candidate, root) != 0 or not candidate.is_file():
        candidate.unlink(missing_ok=True)
        raise RuntimeError(f"the {phase} phase did not pass; the gate record was not changed")
    os.replace(candidate, report)
    bound = sorted(set(record.get("source_sha256") or {}) | files(phase, root))
    refreshed = {**record,
                 "source_sha256": {name: digest(root / name) for name in bound if (root / name).is_file()},
                 "previous_report": {"path": record.get("report_path"), "sha256": record.get("report_sha256")},
                 "report_path": report_path, "report_sha256": digest(report),
                 "refreshed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "environment": {"python": platform.python_version(), "platform": platform.platform()}}
    path.write_text(json.dumps(refreshed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return freshness(path, root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--require-fresh", nargs="+", type=Path)
    mode.add_argument("--refresh-gate")
    args = parser.parse_args(argv)
    if args.refresh_gate:
        try:
            print(json.dumps(refresh_gate(args.refresh_gate), indent=1))
        except RuntimeError as error:
            print(str(error), file=sys.stderr)
            return 1
        return 0
    if args.require_fresh:
        rows = [freshness(path if path.is_absolute() else ROOT / path) for path in args.require_fresh]
        print(json.dumps(rows, indent=1))
        return 0 if all(row["state"] == "fresh" for row in rows) else 1
    print(json.dumps(summary(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
