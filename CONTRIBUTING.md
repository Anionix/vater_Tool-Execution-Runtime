# Contributing

Changes should reference the stable task identifier in the issue title, for example `task.scope.015`.

For each change, include:
1. The acceptance condition and security/trust boundary.
2. Tests with normal and intentionally invalid inputs.
3. Reproducible commands and honest pass/fail/skip results.
4. Evidence tied to the exact changed source version.

**State contract:** `not_started -> in_progress -> review -> done`; the final transition requires successful, relevant, reproducible evidence. A passing mock test is not proof of real protocol interoperability.

The repository is distributed under the [MIT License](LICENSE). Only contribute code and dependencies you have the right to publish under their applicable licenses.
