#!/usr/bin/env python3
"""Fail-closed audit of a generated, hash-pinned Python dependency lock.

Machine contract LOCK-001:
  ABSENT -> REJECTED; READ -> PARSE -> {VALIDATED, REJECTED}.
LOCK-002: each package version is exact and has a SHA-256 artifact digest.
LOCK-003: official MCP SDK version is fixed and all mandatory groups exist.
LOCK-004: no indexes, editable paths, remote references or recursive includes.

This checks lock *syntax* and mandatory entries only. Installing on a clean
Python 3.13 environment with --require-hashes and running protocol tests are
separate required gates. Never treat this script alone as full validation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Sequence

# UV's --generate-hashes requirements format: pinned package, continued hashes.
PACKAGE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9_.!+\-]*)")
HASH = re.compile(r"--hash=sha256:([a-fA-F0-9]{64})")
NORMALIZE = re.compile(r"[-_.]+")
REQUIRED = frozenset({"mcp", "anyio", "pytest", "pydantic", "jsonschema"})


class LockRejected(ValueError):
    """Known failure with a stable code and no untrusted line contents."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonical_name(value: str) -> str:
    return NORMALIZE.sub("-", value).lower()


def _logical_records(source: str) -> list[list[str]]:
    """Recognize line continuation exactly; never run an input as a shell script."""
    records: list[list[str]] = []
    current: list[str] = []
    for original in source.splitlines():
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        continuing = line.endswith("\\")
        line = line[:-1].strip() if continuing else line
        if not line:
            raise LockRejected("LOCK-INVALID-CONTINUATION")
        current.append(line)
        if not continuing:
            records.append(current)
            current = []
    if current:
        raise LockRejected("LOCK-UNFINISHED-CONTINUATION")
    return records


def validate_lock(source: str) -> dict[str, str]:
    """Reject partial, direct-URL or unhashed entries: no implicit defaults."""
    if type(source) is not str or not source.strip():
        raise LockRejected("LOCK-EMPTY")
    packages: dict[str, str] = {}
    for record in _logical_records(source):
        # A generated hash lock contains one pinned package and one or more
        # SHA-256 hashes. Additional tokens are forbidden, not silently ignored.
        tokens = " ".join(record).split()
        match = PACKAGE.fullmatch(tokens[0])
        if match is None:
            raise LockRejected("LOCK-NONPINNED-OR-UNSAFE-REQUIREMENT")
        key = canonical_name(match.group(1))
        if key in packages:
            raise LockRejected("LOCK-DUPLICATE-PACKAGE")
        digests = set()
        for token in tokens[1:]:
            hashed = HASH.fullmatch(token)
            if hashed is None:
                raise LockRejected("LOCK-MISSING-OR-INVALID-HASH")
            digests.add(hashed.group(1).lower())
        if not digests:
            raise LockRejected("LOCK-MISSING-OR-INVALID-HASH")
        packages[key] = match.group(2)
    if not REQUIRED.issubset(packages):
        raise LockRejected("LOCK-MISSING-REQUIRED-GROUP")
    if packages["mcp"] != "2.3.0":
        raise LockRejected("LOCK-WRONG-MCP-VERSION")
    return packages


def verify_file(path: Path) -> dict[str, str]:
    try:
        # Avoid unbounded reading of untrusted dependency input.
        with path.open("rb") as reader:
            data = reader.read(1_048_577)
    except OSError as exc:
        raise LockRejected("LOCK-UNAVAILABLE") from exc
    if len(data) > 1_048_576:
        raise LockRejected("LOCK-TOO-LARGE")
    try:
        source = data.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise LockRejected("LOCK-INVALID-ENCODING") from exc
    return validate_lock(source)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate all Python lock entries and their hashes")
    parser.add_argument("path", nargs="?", type=Path, default=Path("requirements-py313.lock"))
    args = parser.parse_args(argv)
    try:
        packages = verify_file(args.path)
    except LockRejected as exc:
        print(json.dumps({"state": "rejected", "rule": exc.code}, sort_keys=True))
        return 1
    print(json.dumps({"state": "validated", "package_count": len(packages),
                      "official_mcp_version": packages["mcp"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
