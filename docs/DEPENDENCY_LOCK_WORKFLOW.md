# Python 3.13 dependency-lock verification

**State: Python 3.13 Linux lock committed, hash-checked install passed, real MCP 14/14 passed. Full production release gates remain open.**

## What this protects

The official Python MCP SDK and its transitive dependencies are now pinned for Python 3.13 Linux in [requirements-py313.lock](../requirements-py313.lock): **33 distributions and 485 SHA-256 artifact hashes**. The committed file SHA-256 is `cc651303afe6b684a979e14fdd9d1ca8b73d7bda0ea6e1d0480a20ae792de51c`.

## Generate a candidate

Open **Actions → Python dependency lock candidate** in this repository. The workflow also starts after changes to its input files.

It uses CPython 3.13 for x86-64 Linux, resolves the production, test and MCP packages with uv 0.10.0, and generates a candidate requirements-py313.lock containing exact versions and SHA-256 artifact hashes. It then:

1. Rejects missing hashes, unpinned packages, ambiguous package names, unknown directives, editable installs and direct URLs.
2. Installs the resolved dependencies into a fresh virtual environment, requiring hashes for the package artifacts.
3. Checks installed dependency compatibility and the exact official MCP package version.
4. Executes the real SDK communication tests separately from mock tests.
5. Uploads an **unreviewed candidate** file to the run's artifacts, even if a later check fails.

**Completed on 2026-11-24 (Japan time):** [candidate generation and real SDK testing succeeded](https://github.com/Anionix/vater_Tool-Execution-Runtime/actions/runs/45277761135). The generated lock was independently checked against the candidate and committed. [The subsequent main-branch lock-enforced integration run also succeeded](https://github.com/Anionix/vater_Tool-Execution-Runtime/actions/runs/45278396997), including 14 executed official MCP cases with zero failures and zero skips.

## Strict release gates

- Without an accepted requirements-py313.lock, the ordinary Python verification workflow's official-mcp job **fails**, rather than claiming success.
- The lock validator is a syntax/structure gate, not by itself a complete proof of dependency closure.
- Linux/Python 3.13 coverage does not prove Windows, macOS or another interpreter version.
- Rust, Lean and Kani toolchains need their own future locks.

## Commands

Use the local validator and its tests:

    python scripts/check_dependency_lock.py requirements-py313.lock
    python -m pytest -q tests/test_dependency_lock.py

Status machine:

    NO_LOCK -> CANDIDATE -> HASH_VERIFIED -> SDK_REAL_TESTED -> REVIEWED_LOCK -> RELEASE_REVIEW

At each failed step the status becomes BLOCKED, never DONE.

## Primary references

- https://docs.astral.sh/uv/pip/compile/
- https://pypi.org/project/mcp/2.3.0/
- https://github.com/modelcontextprotocol/python-sdk/releases
