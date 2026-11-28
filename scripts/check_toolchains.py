#!/usr/bin/env python3
"""task.operations.001: the Python test environment and the formal-proof toolchains are separate.

  python scripts/check_toolchains.py [--require-locked-python]

Python: every distribution in requirements-py313.lock must be installed at its
locked version for a production candidate; --require-locked-python fails
otherwise. Formal tools (Lean 4.19.0, Kani) are only reported: a tool that is
missing or at another version is 'not_installed' or 'version_mismatch', and
this script never runs a proof, so it never reports a formal result as passed.
The recorded Lean evidence is checked separately (evidence/formal/SHA256SUMS).
"""
from __future__ import annotations

import argparse
from importlib import metadata
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LEAN_VERSION = "4.19.0"


def locked_versions(lock: Path = ROOT / "requirements-py313.lock") -> dict[str, str]:
    found = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==(\S+)", line)
        if match:
            found[re.sub(r"[-_.]+", "-", match.group(1)).lower()] = match.group(2)
    return found


def installed_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def python_environment(installed=None) -> dict:
    installed = installed or installed_version
    mismatches = [{"name": name, "locked": version, "installed": installed(name)}
                  for name, version in sorted(locked_versions().items()) if installed(name) != version]
    return {"locked": not mismatches, "mismatches": mismatches}


def tool(name: str, expected: str | None = None) -> dict:
    path = shutil.which(name)
    if path is None:
        return {"state": "not_installed"}
    try:
        output = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"state": "unusable", "detail": type(error).__name__}
    match = re.search(r"(\d+\.\d+\.\d+)", output)
    version = match.group(1) if match else None
    if expected is not None and version != expected:
        return {"state": "version_mismatch", "version": version, "expected": expected}
    return {"state": "available", "version": version}


def report() -> dict:
    return {"python": python_environment(),
            "formal": {"lean": tool("lean", expected=LEAN_VERSION), "kani": tool("cargo-kani")},
            # Toolchains are only reported here; no proof is run, so no proof result is claimed.
            "formal_proofs_run_here": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-locked-python", action="store_true")
    args = parser.parse_args(argv)
    result = report()
    print(json.dumps(result, indent=1))
    return 1 if args.require_locked_python and not result["python"]["locked"] else 0


if __name__ == "__main__":
    sys.exit(main())
