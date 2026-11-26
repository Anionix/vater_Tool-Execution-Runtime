#!/usr/bin/env python3
"""The minimum local release launcher, not a sandbox.

LOCAL-LAUNCH-001: HOST_POLICY -> VALIDATED -> GUARDED_PROCESS | REJECTED.
LOCAL-LAUNCH-002: the host-owned limits are the only framing settings.
LOCAL-LAUNCH-003: no inherited credentials, recorded-tool switch, arbitrary
child command or caller-controlled argument may enter this release profile.
Run with python -I to ignore ambient Python import configuration on startup.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def verify_local_tool(root: Path) -> None:
    """Check the approved built-in before importing the runtime package."""
    approval = json.loads((root / "config/local_tool.json").read_text(encoding="utf-8"))
    required = {"src/verified_execution/__init__.py", "src/verified_execution/core.py",
                "src/verified_execution/runtime.py", "src/verified_execution/tools.py",
                "server_mcp.py", "contracts/checked_add.input.schema.json",
                "contracts/checked_add.output.contract.json", "contracts/tool_risks.json"}
    if (approval["publisher"] != "Anionix/vater_Tool-Execution-Runtime"
            or approval["name"] != "checked_add" or set(approval["sha256"]) != required):
        raise ValueError("INVALID_LOCAL_TOOL")
    for relative in required:
        with (root / relative).open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != approval["sha256"][relative]:
                raise ValueError("LOCAL_TOOL_CHANGED")
    contract = json.loads((root / "contracts/checked_add.output.contract.json").read_text(encoding="utf-8"))
    if contract["contract_version"] != approval["version"] or contract["tool"] != approval["name"]:
        raise ValueError("INVALID_LOCAL_TOOL_VERSION")


if __name__ == "__main__":
    # task.authorization.140: host-owned registration -> verify -> import -> serve.
    try:
        verify_local_tool(ROOT)
    except (OSError, KeyError, TypeError, ValueError):
        print("LOCAL_TOOL_REJECTED", file=sys.stderr)
        raise SystemExit(1)

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
