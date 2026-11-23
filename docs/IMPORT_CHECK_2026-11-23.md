# Public import verification — 2026-11-23

## Scope
This is a point-in-time check of the **Python-first VATER prototype**, its published task ledger and the public GitHub source. It does **not** certify a release, formal proof, or MCP interoperability.

## Confirmed GitHub content
- `backlog/tasks.jsonl`: **351 unique task IDs**, comprising 332 individual tasks and 19 phase gates.
- `backlog/TASKS.md`: human-readable list with 351 stable IDs.
- `src/verified_execution/core.py`, `ledger.py`, `server_mcp.py`, `pyproject.toml` and selected tests exist in this repository.
- The Git blob checksums for the main source, task ledger, and schema match the original prepared publication package.
- `contracts/expected_cases.jsonl` and `config/limits.json` are present. They were missing from the first publication package.
- `backlog/validate_tasks.py` uses the published `docs/sources.jsonl` and repository-root `evidence/` paths.

## Measured local verification
- Reconstructed the publication package locally using Python **3.13.5**.
- Task-ledger structural check: **valid**, 351 records, 17 formally completed in the stored snapshot, 19 incomplete phase gates.
- Selected independent Python smoke suite: **109 passed; 0 failed; 0 errors; 0 skipped**.
- Source compilation check passed.
- The workflow `.github/workflows/python-validation.yml` was added to run these checks on GitHub-hosted runners. Its **actual runner outcome is not asserted here**.

## Remaining verification gaps
- Official Python MCP client/server wire compatibility and cancellation are not verified.
- `backlog/tools/validate_redesign.py` and the archived migration/reconstruction suites reference `backlog/history/prior_tasks.jsonl` and historical directory paths which are not in the published repository. They are **not** part of the above 109 tests, and their migration proof must not be marked complete.
- These are bounded smoke tests, not long-duration durability or security certification.
- No redistribution license has yet been selected.

## Machine-readable checks
Use from the repository root:

```bash
python backlog/validate_tasks.py
python -m pytest -q -rs tests/test_core.py tests/test_contracts.py \
  tests/test_adapter_boundary.py tests/test_recorded_calculator.py \
  tests/test_recorded_tool_bridge.py tests/test_cancellation_journal.py
```

**State rule:** planned -> implemented -> tested -> reviewed -> done. A task is not done solely because a file exists or an Issue was created.
