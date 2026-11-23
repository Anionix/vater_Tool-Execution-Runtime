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

Some tests require the official Python MCP package, are explicitly skipped when unavailable, and **must not** be counted as protocol validation. The development package is currently unpinned for transitive dependencies; see [dependency status](docs/DEPENDENCY_LOCK_STATUS.md).

## GitHub Issues

**38 of 351 planned Issues** are registered. Additional Issues can be prepared and checked without writing to GitHub:

```bash
python scripts/import_github_issues.py --repo Anionix/vater_Tool-Execution-Runtime --all
```

Importing with `--execute` performs remote writes and requires the separately installed GitHub command-line tool and account access. The script deduplicates by immutable task identifier, never closes completed tasks automatically, and creates only a limited batch by default.

[Current Issues](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues)

## Security and legal

- Do **not** expose this prototype to untrusted remote callers or privileged credentials.
- Read [SECURITY.md](SECURITY.md) for current limitations.
- Rust, Lean, Kani, and other interface adapters are optional later stages.
- **No open-source license has been selected.** A public repository does not automatically grant redistribution permission.
