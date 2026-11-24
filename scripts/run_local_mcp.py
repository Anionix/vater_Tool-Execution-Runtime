#!/usr/bin/env python3
"""The minimum local release launcher, not a sandbox.

LOCAL-LAUNCH-001: HOST_POLICY -> VALIDATED -> GUARDED_PROCESS | REJECTED.
LOCAL-LAUNCH-002: the host-owned limits are the only framing settings.
LOCAL-LAUNCH-003: no inherited credentials, recorded-tool switch, arbitrary
child command or caller-controlled argument may enter this release profile.
Run with python -I to ignore ambient Python import configuration on startup.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from verified_execution.policy_config import PolicyConfigurationError, load_local_policy


def launch_spec(root: Path, executable: str) -> tuple[list[str], dict[str, str]]:
    """Build a fixed command and a fresh child environment; no ambient reads."""
    root = root.resolve()
    limits = load_local_policy(root / "config" / "limits.json")
    command = [
        executable, "-I", "-u", str(root / "scripts" / "guarded_stdio.py"),
        "--max-bytes", str(limits.request_input_max_bytes),
        "--max-depth", str(limits.json_max_depth),
        "--max-output-bytes", str(limits.request_output_max_bytes),
        "--max-output-depth", str(limits.json_max_depth),
        "--", executable, "-u", str(root / "server_mcp.py"),
    ]
    # Never copy os.environ. The trusted child needs only the local source path.
    environment = {
        "PYTHONPATH": str(root / "src"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "C.UTF-8",
    }
    return command, environment


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv or os.name != "posix":
        print("LOCAL_LAUNCH_UNSUPPORTED_ARGUMENT_OR_PLATFORM", file=sys.stderr)
        return 2
    try:
        command, environment = launch_spec(ROOT, os.path.abspath(sys.executable))
        # Replace the launcher: the existing guard owns signals and cleanup.
        os.execve(command[0], command, environment)
    except (PolicyConfigurationError, OSError):
        print("LOCAL_LAUNCH_REJECTED", file=sys.stderr)
        return 1
    return 1  # execve never returns on success


if __name__ == "__main__":
    raise SystemExit(main())
