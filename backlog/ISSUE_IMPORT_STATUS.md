# GitHub Issue import checkpoint — VATER

Snapshot (2026-11-23, Japan time). GitHub issue imports may be concurrent; refresh before each later batch.

- Original plan: **351 unique identifiers** (332 tasks, 19 phase gates).
- Registered: **63 unique identifiers** (individual tasks 44, gates 19).
- Not yet registered: **288**.
- Open / closed canonical issues: **46 / 17**.
- Duplicate registrations: **2** (excluded from unique progress). GitHub does not provide an idempotency key for Issue creation.
- Last audited task completion: **17 / 351**. This is *different* from the issue import count.
- Official MCP live communication: **unverified**. Historic [Python test report](https://github.com/Anionix/vater_Tool-Execution-Runtime/blob/main/evidence/verification_0_11.json): 289 passed, 14 skipped. Not rerun here.
- Release readiness: **BLOCKED**.

## Canonical mapping (lowest issue number for each task identifier)

| Issue | Immutable task identifier | State |
|---|---|---|
| [#1](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/1) | `gate.scope.completed` | open |
| [#2](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/2) | `gate.trust.completed` | open |
| [#3](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/3) | `gate.contracts.completed` | open |
| [#4](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/4) | `gate.runtime.completed` | open |
| [#5](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/5) | `gate.mcp_transport.completed` | open |
| [#6](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/6) | `gate.authorization.completed` | open |
| [#7](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/7) | `gate.read_only.completed` | open |
| [#8](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/8) | `gate.minimum_validation.completed` | open |
| [#9](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/9) | `gate.minimum_release.completed` | open |
| [#10](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/10) | `gate.provider_adapters.completed` | open |
| [#11](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/11) | `gate.http_interface.completed` | open |
| [#12](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/12) | `gate.durable_operations.completed` | open |
| [#13](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/13) | `gate.testing.completed` | open |
| [#14](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/14) | `gate.operations.completed` | open |
| [#15](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/15) | `gate.formal_verification.completed` | open |
| [#16](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/16) | `gate.optional_webmcp.completed` | open |
| [#17](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/17) | `gate.optional_webassembly.completed` | open |
| [#18](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/18) | `gate.optional_webgpu.completed` | open |
| [#19](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/19) | `gate.optional_more_protocols.completed` | open |
| [#20](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/20) | `task.scope.015` | open |
| [#21](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/21) | `task.trust.001` | open |
| [#22](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/22) | `task.trust.002` | open |
| [#23](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/23) | `task.trust.003` | open |
| [#24](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/24) | `task.trust.004` | open |
| [#25](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/25) | `task.trust.005` | open |
| [#26](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/26) | `task.trust.006` | open |
| [#27](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/27) | `task.trust.007` | open |
| [#28](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/28) | `task.trust.008` | open |
| [#29](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/29) | `task.trust.009` | open |
| [#30](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/30) | `task.trust.010` | open |
| [#31](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/31) | `task.trust.011` | open |
| [#32](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/32) | `task.trust.012` | open |
| [#33](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/33) | `task.trust.013` | open |
| [#34](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/34) | `task.trust.014` | open |
| [#35](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/35) | `task.trust.015` | open |
| [#36](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/36) | `task.trust.016` | open |
| [#37](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/37) | `task.trust.017` | open |
| [#38](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/38) | `task.trust.018` | open |
| [#39](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/39) | `task.scope.001` | closed |
| [#40](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/40) | `task.scope.002` | closed |
| [#41](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/41) | `task.scope.003` | closed |
| [#42](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/42) | `task.scope.004` | closed |
| [#43](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/43) | `task.scope.005` | closed |
| [#44](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/44) | `task.scope.006` | closed |
| [#45](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/45) | `task.scope.007` | closed |
| [#46](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/46) | `task.scope.008` | closed |
| [#47](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/47) | `task.scope.009` | closed |
| [#48](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/48) | `task.scope.010` | closed |
| [#49](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/49) | `task.scope.011` | closed |
| [#50](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/50) | `task.scope.012` | closed |
| [#51](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/51) | `task.scope.013` | closed |
| [#52](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/52) | `task.scope.014` | closed |
| [#53](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/53) | `task.scope.016` | closed |
| [#54](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/54) | `task.scope.017` | closed |
| [#55](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/55) | `task.scope.018` | closed |
| [#58](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/58) | `task.contracts.001` | open |
| [#59](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/59) | `task.contracts.002` | open |
| [#60](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/60) | `task.contracts.003` | open |
| [#61](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/61) | `task.contracts.004` | open |
| [#62](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/62) | `task.contracts.005` | open |
| [#63](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/63) | `task.contracts.006` | open |
| [#64](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/64) | `task.contracts.007` | open |
| [#65](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/65) | `task.contracts.008` | open |

## Duplicate issues

- [#57](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/57) corresponds to `task.scope.018`; canonical issue is [#55](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/55)
- [#56](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/56) corresponds to `task.scope.017`; canonical issue is [#54](https://github.com/Anionix/vater_Tool-Execution-Runtime/issues/54)

## Import contract

1. Load [the full 351-record source](https://github.com/Anionix/vater_Tool-Execution-Runtime/blob/main/backlog/tasks.jsonl) and [issue body proposals](https://github.com/Anionix/vater_Tool-Execution-Runtime/blob/main/backlog/github_issues.jsonl).
2. Search existing GitHub issue identifiers before creating each issue. Do not rely solely on a previously generated checkpoint, because concurrent writers may create an issue between the check and the write.
3. Use only one active importer at a time; GitHub Issue creation does **not** guarantee atomic deduplication. Mark any races as duplicates and use the lowest-numbered canonical issue.
4. Preserve the task identifier, objective, acceptance conditions, dependencies, verified evidence and latest source status.
5. Keep phase completion gates independent from individual task completion.
