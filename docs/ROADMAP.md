# VATER roadmap — original Python-first plan

The versioned planning ledger contains **351 stable task records**: **332 individual tasks and 19 phase completion gates**. A previous local audit certified 17 completed; verify any later changes against evidence rather than assuming more work is done.

## Initial release: mandatory phases

1. Scope and non-goals (`scope`)
2. Threat model and trust boundaries (`trust`)
3. Contracts and data meaning (`contracts`)
4. Python execution runtime (`runtime`)
5. MCP transport and real interoperability (`mcp_transport`)
6. Authorization decisions (`authorization`)
7. Read-only end-to-end vertical implementation (`read_only`)
8. Independent minimum validation (`minimum_validation`)
9. Minimum release gate (`minimum_release`)

## Follow-on and optional phases

- Provider adapters (`provider_adapters`)
- REST and OpenAPI (`http_interface`)
- Durable state and side effects (`durable_operations`)
- Broader testing (`testing`)
- Operations and delivery (`operations`)
- Formal verification / Rust migration (`formal_verification`)
- Browser / WebMCP (`optional_webmcp`)
- WebAssembly sandboxing (`optional_webassembly`)
- WebGPU acceleration (`optional_webgpu`)
- Further protocols (`optional_more_protocols`)

Formal verification and Rust are **not mandatory for the initial Python-first release**. A completed issue does not automatically complete a phase gate. Each phase gate needs independent evidence, dependencies, and reproducible results.

## Known blockers

- Pinning all official Python MCP dependencies with reproducible hashes (`task.scope.015`).
- Official Python MCP client/server real-transport and cancellation verification remains unexecuted, even if offline tests pass.
- Production authentication, external side effects and independent audit remain incomplete.

## Issue conventions

Each issue title includes an immutable `task.<phase>.<number>` or `gate.<phase>.completed` identifier. This avoids losing traceability when implementation languages or acceptance conditions change.
