// Run with the official conformance checkout.s tsx loader and the locked Python interpreter.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

const [upstream, python] = process.argv.slice(2);
const { ToolsListScenario } = await import(pathToFileURL(resolve(upstream, "src/scenarios/server/tools.ts")));
const { wireSchemaErrors } = await import(pathToFileURL(resolve(upstream, "src/validation/wire-schema.ts")));

for (const specVersion of ["2025-11-25", "2026-07-28"]) {
  const child = spawn(python, ["-I", "scripts/run_local_mcp.py"], {
    stdio: ["pipe", "pipe", "inherit"],
  });
  const exited = once(child, "exit");
  const timeout = setTimeout(() => child.kill("SIGKILL"), 12000);
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  let id = 0;
  const request = async (method, parameters = {}) => {
    const params = specVersion === "2026-07-28" ? {
      ...parameters, _meta: {
        "io.modelcontextprotocol/protocolVersion": specVersion,
        "io.modelcontextprotocol/clientCapabilities": {},
      },
    } : parameters;
    const frame = { jsonrpc: "2.0", id: ++id, method, params };
    assert.deepEqual(wireSchemaErrors(specVersion, frame), [], method);
    child.stdin.write(JSON.stringify(frame) + "\n");
    const line = await lines.next();
    assert.equal(line.done, false, method);
    const response = JSON.parse(line.value);
    assert.equal(response.id, frame.id);
    assert.deepEqual(wireSchemaErrors(specVersion, response, method), [], method);
    assert.equal(response.error, undefined, method);
    return response.result;
  };
  try {
    if (specVersion === "2025-11-25") {
      const initialized = await request("initialize", {
        protocolVersion: specVersion, capabilities: {},
        clientInfo: { name: "vater-conformance", version: "1" },
      });
      assert.equal(initialized.protocolVersion, specVersion);
      child.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n');
    } else {
      await request("server/discover");
    }
    const checks = await new ToolsListScenario().run({
      specVersion,
      connect: async () => ({ request, close: async () => {} }),
    });
    assert.ok(checks.length > 0);
    assert.ok(checks.every(check => check.status === "SUCCESS" || (check.id === "tools-list-deterministic-order" && check.status === "INFO" && check.details.toolCount === 1)), JSON.stringify(checks));
    const success = await request("tools/call", { name: "checked_add", arguments: { left: 1, right: 2 } });
    assert.deepEqual(success.structuredContent, { result: 3 });
    const rejected = await request("tools/call", { name: "checked_add", arguments: { left: 1 } });
    assert.equal(rejected.isError, true);
    assert.notEqual(wireSchemaErrors(specVersion, { jsonrpc: "2.0", id: 1, result: { tools: null } }, "tools/list").length, 0);
    console.log(JSON.stringify({ specVersion, checks, wireChecks: "passed" }));
  } finally {
    child.stdin.end();
    const [code, signal] = await exited;
    clearTimeout(timeout);
    assert.equal(signal, null);
    assert.equal(code, 0);
  }
}
