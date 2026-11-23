# Official MCP diagnostic verification

**Status: diagnostic automation added; result not independently observed.**

The repository now has TWO different paths to official MCP verification, deliberately kept separate.

## Diagnostic smoke test: useful before dependency locking

[Official MCP diagnostic smoke test](../.github/workflows/official-mcp-smoke.yml) runs automatically after relevant source changes, or manually through the Actions page.

- Python 3.13 on GitHub-hosted Linux
- Installs the project with the test and MCP extras; pins the main MCP SDK to `2.3.0`
- Checks the installed distribution version
- Runs the existing real in-process and standard-input/output client/server tests
- Requires a real test report with **at least 14 executed tests**, **zero skips**, **zero failures**, and **zero errors**
- Uploads the raw report, including on failure

Its dependencies are **not fully hash-pinned**. Even if every test passes, this result is **diagnostic only**, not authorization to release. The runtime’s Python source and tests are unchanged by this workflow.

## Release validation: requires reviewed hashes

The normal [Python verification](../.github/workflows/python-verification.yml) job refuses to start official SDK tests without a reviewed `requirements-py313.lock`, whose package artifact hashes are verified during installation.

The [dependency-lock candidate workflow](../.github/workflows/dependency-lock-candidate.yml) attempts to create the lock from the real package registry and test a clean installation. A candidate must be reviewed before merging it into the repository. This is a separate release gate.

## State machine

`UNRUN -> DIAGNOSTIC_RUNNING -> {DIAGNOSTIC_PASS | DIAGNOSTIC_FAIL}`

`DIAGNOSTIC_PASS` **does not transition to** `RELEASE_APPROVED`. Release requires a reviewed dependency lock, the full official SDK test run in the locked environment, and the remaining documented security gates.

## Failure checklist

- Dependency resolution fails: inspect package availability and Python compatibility; do not fake hashes
- The official SDK is absent or wrong version: diagnostic fails immediately
- A test fails or is skipped: the report validator rejects the suite
- The report is missing or malformed: the report validator rejects it
- Workflow cannot run: mark as `NOT_EXECUTED`, not `PASS`

Primary references: [official Python MCP package](https://pypi.org/project/mcp/2.3.0/) and [official uv dependency locking guide](https://docs.astral.sh/uv/pip/compile/).
