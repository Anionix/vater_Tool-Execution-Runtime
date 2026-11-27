#!/usr/bin/env python3
"""task.operations.012: mechanical audit of logs and evidence for secret values.

  python scripts/audit_logs.py evidence/ server.log --secret-env GITHUB_TOKEN

Scans every text file under the given paths for credential shapes (GitHub,
Stripe-like and AWS keys, bearer headers, private keys, JWTs, password or
token assignments) and, literally, for the values of the named environment
variables. Any finding fails the audit (exit status 1). Matches are printed
redacted, so the audit's own output never repeats a secret.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

RULES = {
    "github_token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
    "stripe_like_key": re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "bearer_header": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    "private_key": re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "credential_assignment": re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[=:]\s*['\"]?"
        r"(?!\*|<|\$\{|redacted|REDACTED|null|None|true|false)[^\s'\",}]{8,}"),
}
TEXT_SUFFIXES = {".log", ".txt", ".json", ".jsonl", ".xml", ".md", ".out", ".err", ".yml", ".yaml", ".csv"}
MAX_BYTES = 20_000_000


def redact(value: str) -> str:
    return f"{value[:4]}…({len(value)} chars)" if len(value) > 4 else "…"


def files_under(paths: list[Path]):
    for path in paths:
        if path.is_file():
            yield path
        elif path.is_dir():
            yield from (child for child in sorted(path.rglob("*"))
                        if child.is_file() and child.suffix.lower() in TEXT_SUFFIXES)


def audit_text(text: str, secrets: list[str]) -> list[tuple[int, str, str]]:
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        for rule, pattern in RULES.items():
            for match in pattern.finditer(line):
                findings.append((number, rule, redact(match.group(0))))
        for secret in secrets:
            if secret and secret in line:
                findings.append((number, "known_secret_value", redact(secret)))
    return findings


def audit(paths: list[Path], secrets: list[str]) -> dict:
    report = {"files_scanned": 0, "findings": []}
    for path in files_under(paths):
        if path.stat().st_size > MAX_BYTES:
            report["findings"].append({"path": str(path), "line": 0, "rule": "too_large_to_audit", "excerpt": ""})
            continue
        report["files_scanned"] += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, rule, excerpt in audit_text(text, secrets):
            report["findings"].append({"path": str(path), "line": number, "rule": rule, "excerpt": excerpt})
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--secret-env", action="append", default=[],
                        help="name of an environment variable whose value must never appear")
    args = parser.parse_args(argv)
    secrets = [os.environ[name] for name in args.secret_env if len(os.environ.get(name, "")) >= 8]
    report = audit(args.paths, secrets)
    print(json.dumps(report, indent=1))
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
