# Evidence provenance and limits

This repository was migrated from a local Python-first VATER project snapshot dated **2026-11-23**.

- The canonical task ledger is `backlog/tasks.jsonl`, with **351 records** and stable `task_id` values.
- The historical snapshot contains **17 tasks marked done**, based on local evidence under `evidence/`.
- Some `source_sha256` entries in historical evidence refer to former package paths (e.g. `application/src/...`), now at `src/...`; do **not** treat these historical checksums as hashes of the new GitHub commit.
- Historical test reports and claims are not a substitute for independently rerunning tests against a specific Git commit.
- The official Python MCP integration tests were **not** executed when this snapshot was prepared. A local mock passing does not imply MCP standard conformance.
- The project is experimental, **MIT-licensed** (see [LICENSE](../LICENSE)), and **not approved for production**.

To revalidate, install locally available dependencies and run the appropriate scripts from `scripts/`. Report exact versions, commands, passed, failed, and skipped counts. Never silently count skipped checks as success.

**State transition contract:** not_started → in_progress → review → done, with task-specific relevant evidence and predecessor gate checks before completion.
