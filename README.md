# VATER — Verifiable Agent Tool Execution Runtime

**Python-first experimental tool execution runtime for AI agents and other clients.**

The core checks inputs and authorization, tracks durable operation state, and handles cancellation. MCP is the first protocol adapter; other interfaces are future options.

> **Development status: experimental — not production-ready.** Official MCP client/server interoperability remains unverified. Passing local Python tests is not proof of protocol compliance or full security.

## Design

1. Define tool input/output contracts.
2. Validate the request and enforce host-owned permissions.
3. Execute the tool and record its result or uncertainty.
4. Test both success and failure, preserving reproducible evidence.
5. Port only justified performance/security-critical components to Rust; use formal verification where useful.

## Project tracking

The Python-first plan has **351 records**: 332 tasks and 19 phase completion gates. The last audited ledger marked 17 completed; this is a historical snapshot, not an assertion that production readiness has been achieved.

- [Phase-level work and issues](../../issues)
- [Project status and planned import](../../issues?q=is%3Aissue)
- See the backlog and full source package maintained with the project; source import may be incremental.

## Security and availability

Do **not** expose the prototype to untrusted remote callers or privileged credentials. Authentication, official MCP interoperability, and external-side-effect reconciliation have not passed production gates.

## License

**No open-source license has been selected.** Public visibility does not imply permission to redistribute or reuse the software.

## Name

**VATER** stands for **Verifiable Agent Tool Execution Runtime**.
