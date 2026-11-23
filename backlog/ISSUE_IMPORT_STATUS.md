# GitHub Issue import checkpoint (historical first batch)

**Repository:** `Anionix/vater_Tool-Execution-Runtime`

The **first import batch** created 38 records (19 phase gates and 19 individual tasks). **This is a historical checkpoint, not the current total.** More Issues may have been created since this snapshot; view [live Issues](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues?q=is%3Aissue) and reconcile by immutable task identifier before further imports. Closed duplicate Issues must not be counted as additional work items.

This table records only the initial batch. Before importing another batch, search all open **and closed** Issues for the stable task identifier in the title; do not create duplicates. Creating an Issue **never** means the task is done.

| GitHub Issue | Stable task identifier |
|---|---|
| [#1](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/1) | `gate.scope.completed` |
| [#2](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/2) | `gate.trust.completed` |
| [#3](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/3) | `gate.contracts.completed` |
| [#4](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/4) | `gate.runtime.completed` |
| [#5](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/5) | `gate.mcp_transport.completed` |
| [#6](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/6) | `gate.authorization.completed` |
| [#7](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/7) | `gate.read_only.completed` |
| [#8](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/8) | `gate.minimum_validation.completed` |
| [#9](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/9) | `gate.minimum_release.completed` |
| [#10](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/10) | `gate.provider_adapters.completed` |
| [#11](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/11) | `gate.http_interface.completed` |
| [#12](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/12) | `gate.durable_operations.completed` |
| [#13](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/13) | `gate.testing.completed` |
| [#14](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/14) | `gate.operations.completed` |
| [#15](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/15) | `gate.formal_verification.completed` |
| [#16](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/16) | `gate.optional_webmcp.completed` |
| [#17](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/17) | `gate.optional_webassembly.completed` |
| [#18](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/18) | `gate.optional_webgpu.completed` |
| [#19](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/19) | `gate.optional_more_protocols.completed` |
| [#20](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/20) | `task.scope.015` |
| [#21](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/21) | `task.trust.001` |
| [#22](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/22) | `task.trust.002` |
| [#23](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/23) | `task.trust.003` |
| [#24](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/24) | `task.trust.004` |
| [#25](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/25) | `task.trust.005` |
| [#26](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/26) | `task.trust.006` |
| [#27](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/27) | `task.trust.007` |
| [#28](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/28) | `task.trust.008` |
| [#29](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/29) | `task.trust.009` |
| [#30](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/30) | `task.trust.010` |
| [#31](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/31) | `task.trust.011` |
| [#32](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/32) | `task.trust.012` |
| [#33](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/33) | `task.trust.013` |
| [#34](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/34) | `task.trust.014` |
| [#35](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/35) | `task.trust.015` |
| [#36](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/36) | `task.trust.016` |
| [#37](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/37) | `task.trust.017` |
| [#38](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/38) | `task.trust.018` |

## Rules for the next batch

1. Import only tasks with a stable `task_id` from the original 351-item plan.
2. Verify the issue does not already exist by searching the identifier.
3. Preserve the original acceptance condition and predecessor IDs.
4. Mark progress only after linking actual verification evidence.
5. Keep the original Python-first backlog as the canonical task source until all 351 records are imported.

## Publication progress is different from implementation progress

- GitHub Issues: 38 created / 351 planned
- Implementation task ledger: 17 officially completed / 351 planned in the last verified snapshot
- Source code publication: Python sources, tests, contracts and the full backlog imported; independent public review still pending
- Release readiness: **not approved**
- Issue proposal data: all 351 records available in `backlog/github_issues.jsonl`; 313 not yet published as GitHub Issues
- Import safety: `scripts/import_github_issues.py` detects prior Issues using stable task identifiers, not mutable issue titles
