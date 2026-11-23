#!/usr/bin/env python3
"""Zero-dependency Python production-code quality gate.

STATIC-STATE-001: DISCOVER -> PARSE -> CHECK -> PASS | FAIL.
STATIC-001: reject syntax errors and wildcard imports.
STATIC-002: reject direct dynamic-execution and unsafe-deserialization calls.
STATIC-003: reject subprocess execution with shell not literally False.
STATIC-004: prohibit MCP SDK imports in the SDK-independent calculation core.

This is syntactic source analysis, NOT proof of code safety. It deliberately
checks only patterns that can be enforced without network access.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Sequence

DANGEROUS_CALLS = frozenset({
    "eval", "exec", "__import__", "builtins.eval", "builtins.exec",
    "builtins.__import__", "os.system", "os.popen", "pickle.load",
    "pickle.loads", "_pickle.load", "_pickle.loads",
})
PROCESS_CALLS = frozenset({
    "subprocess.Popen", "subprocess.run", "subprocess.call",
    "subprocess.check_call", "subprocess.check_output",
})


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    rule: str


def _call_name(node: ast.expr, imported: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return imported.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value, imported)
        return f"{base}.{node.attr}" if base else None
    return None


def inspect_python(text: str, *, file: str, core: bool) -> list[Finding]:
    """Enforce explicit patterns with stable rule codes, never source contents."""
    try:
        tree = ast.parse(text, filename=file)
    except (SyntaxError, ValueError, RecursionError):
        return [Finding(file, 0, "STATIC-001-SYNTAX")]

    findings: list[Finding] = []
    imported: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for entry in node.names:
                local = entry.asname or entry.name.split(".", 1)[0]
                imported[local] = entry.name if entry.asname else local
                if core and entry.name.split(".", 1)[0] == "mcp":
                    findings.append(Finding(file, node.lineno, "STATIC-004-MCP-CORE"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if core and module.split(".", 1)[0] == "mcp":
                findings.append(Finding(file, node.lineno, "STATIC-004-MCP-CORE"))
            for entry in node.names:
                if entry.name == "*":
                    findings.append(Finding(file, node.lineno, "STATIC-001-WILDCARD"))
                else:
                    imported[entry.asname or entry.name] = f"{module}.{entry.name}"

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func, imported)
        if name in DANGEROUS_CALLS:
            findings.append(Finding(file, node.lineno, "STATIC-002-DANGEROUS-CALL"))
        if name in PROCESS_CALLS:
            for kw in node.keywords:
                if kw.arg == "shell" and not (
                    isinstance(kw.value, ast.Constant) and kw.value.value is False
                ):
                    findings.append(Finding(file, node.lineno, "STATIC-003-SHELL"))
            if any(kw.arg is None for kw in node.keywords):
                # **options could contain shell=True; no proof of safety.
                findings.append(Finding(file, node.lineno, "STATIC-003-UNKNOWN-KEYWORDS"))
    return sorted(set(findings), key=lambda x: (x.file, x.line, x.rule))


def check_project(root: Path) -> tuple[list[Finding], int]:
    """Scan all production modules and the MCP adapter; never scan test fixtures."""
    root = root.resolve()
    core = root / "src" / "verified_execution"
    adapter = root / "server_mcp.py"
    if not core.is_dir() or not adapter.is_file():
        raise ValueError("STATIC-000-MISSING-PRODUCTION-SOURCE")
    modules = sorted(core.rglob("*.py")) + [adapter]
    if not modules:
        raise ValueError("STATIC-000-NO-PRODUCTION-SOURCE")
    findings: list[Finding] = []
    for module in modules:
        relative = module.relative_to(root).as_posix()
        try:
            contents = module.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            findings.append(Finding(relative, 0, "STATIC-001-UNREADABLE"))
            continue
        findings.extend(inspect_python(contents, file=relative, core=module != adapter))
    return sorted(findings, key=lambda x: (x.file, x.line, x.rule)), len(modules)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail on strict production-code violations")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        findings, scanned = check_project(args.root)
    except ValueError as exc:
        print(json.dumps({"state": "fail", "files_scanned": 0,
                          "findings": [{"file": "", "line": 0, "rule": str(exc)}]}))
        return 1
    print(json.dumps({"state": "fail" if findings else "pass", "files_scanned": scanned,
                      "findings": [asdict(x) for x in findings]}, sort_keys=True))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
