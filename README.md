# VATER — Verifiable Agent Tool Execution Runtime

**Experimental Python-first tool execution runtime** for AI agents and other clients. Its current components validate inputs, enforce host-owned authorization, record operation state, and test cancellation. **MCP is the first planned protocol adapter, not the only possible interface.**

> **Not production-ready.** The official Python MCP client/server interoperability tests have not been run successfully in the original environment. Offline Python test results do not establish protocol compliance or a complete security guarantee.

## Repository guide

- [Python runtime](src/verified_execution/) and [MCP adapter](server_mcp.py)
- [Local tests](tests/) and [verification scripts](scripts/)
- [Tool contracts](contracts/) and [runtime limits](config/limits.json)
- [Original 351-task ledger](backlog/tasks.jsonl) — 332 individual tasks + 19 phase completion gates
- [Human-readable backlog](backlog/TASKS.md) and [release roadmap](docs/ROADMAP.md)
- [Local verification evidence](evidence/) and [provenance limitations](docs/PROVENANCE.md)
- [GitHub Issue import checkpoint](backlog/ISSUE_IMPORT_STATUS.md) and [prepared Issue proposals](backlog/github_issues.jsonl)

The last audited planning snapshot records **17/351 tasks completed**, but this is a historical count. GitHub Issue publication is tracked separately from implementation progress.

## Quick local verification

Requires **Python 3.13 or later** and locally installed test dependencies (see [pyproject.toml](pyproject.toml)).

```bash
python -m pip install -e '.[test]'
python -m pytest -q tests/test_core.py tests/test_contracts.py tests/test_recorded_calculator.py
```

To run the broader offline checks:

```bash
./scripts/run_offline_checks.sh
```

Some tests require the official Python MCP package, are explicitly skipped when unavailable, and **must not** be counted as protocol validation. Transitive dependencies still lack an accepted hash-pinned lock. The [candidate generation and clean-install workflow](docs/DEPENDENCY_LOCK_WORKFLOW.md) is implemented, but its official MCP verification is **not yet confirmed**. Until an accepted lock is committed, the official MCP job intentionally fails closed.

## Official MCP verification paths

- [Diagnostic smoke test](.github/workflows/official-mcp-smoke.yml): installs the selected SDK in a temporary GitHub Actions environment and attempts real client/server tests. Diagnostic only; no release approval.
- [Dependency-lock candidate](.github/workflows/dependency-lock-candidate.yml): generates and verifies a candidate with package hashes.
- [Release-oriented verification](.github/workflows/python-verification.yml): requires an accepted hash-pinned dependency lock and rejects skipped official MCP tests.

**The diagnostic workflow has been committed, but its execution result has not yet been verified.** A passing diagnostic alone is not a production release gate.

## GitHub Issues

Issue registration is **complete for all 351 stable task identifiers** (332 tasks and 19 phase gates). There are also seven closed duplicate Issues. Track the canonical GitHub Issue number using [the checked Issue index](backlog/ISSUE_INDEX.jsonl) and [registration checkpoint](backlog/ISSUE_IMPORT_STATUS.md). Additional Issues can be prepared and checked without writing to GitHub:

```bash
python scripts/import_github_issues.py --repo Anionix/vater_Tool-Execution-Runtime --all
```

Importing with `--execute` performs remote writes and requires the separately installed GitHub command-line tool and account access. The script deduplicates by immutable task identifier, never closes completed tasks automatically, and creates only a limited batch by default.

[Current Issues](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues)

## Security and legal

- Do **not** expose this prototype to untrusted remote callers or privileged credentials.
- Read [SECURITY.md](SECURITY.md) for current limitations.
- Rust, Lean, Kani, and other interface adapters are optional later stages.
- **Licensed under the MIT License**; see [LICENSE](LICENSE). The license grants permissions, but does not certify security or production readiness.
