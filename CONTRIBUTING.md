# Contributing

Changes should reference the stable task identifier in the issue title, for example `task.scope.015`.

For each change, include:
1. The acceptance condition and security/trust boundary.
2. Tests with normal and intentionally invalid inputs.
3. Reproducible commands and honest pass/fail/skip results.
4. Evidence tied to the exact changed source version.

**State contract:** `not_started -> in_progress -> review -> done`; the final transition requires successful, relevant, reproducible evidence. A passing mock test is not proof of real protocol interoperability.

The repository is distributed under the [MIT License](LICENSE). Only contribute code and dependencies you have the right to publish under their applicable licenses.

## Reproduce the local acceptance checks

Use `uv venv --python "$(cat .python-version)" .venv` and
`uv pip sync --python .venv/bin/python --require-hashes requirements-py313.lock`.
Run the following checks separately from the repository root.

| Check | Command |
| --- | --- |
| Tests and reusable report | `.venv/bin/python -m pytest -o addopts= -q --junitxml=/tmp/vater-tests.xml` |
| Format preview for changed Python files | `uvx --from ruff==0.14.8 ruff format --diff path/to/changed.py` |
| Production static rules | `.venv/bin/python scripts/check_static_policy.py` |
| Ledger and prerequisites | `.venv/bin/python backlog/validate_tasks.py` |
| Completed phase using the same report | `.venv/bin/python scripts/verify_phase.py contracts --report /tmp/vater-tests.xml` |

A checked-out source tree supplies the standard-I/O launcher and the contract files. The wheel supplies the Python computation package. See [the fixed local launcher](scripts/run_local_mcp.py).
