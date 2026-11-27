// task.testing.022: run the official MCP conformance scenarios against the
// local stdio server, for every protocol version the server's SDK supports,
// and save one result file per version.
//
//   node --import <upstream>/node_modules/tsx/dist/loader.mjs scripts/run_mcp_conformance.mjs \
//        <upstream checkout> <python> <output directory>
//
// The upstream harness speaks HTTP; this runner gives each scenario a
// Connection over standard input and output instead (the interface the
// scenarios are written against), and checks every frame with the upstream
// wire-schema validator. Nothing is filtered here: every scenario that
// upstream applies to a version runs, and its checks are saved as reported.
// Which results are required to pass is decided by
// evidence/mcp-conformance/applicability.json, read by the Python test.
import { execFileSync, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { once } from "node:events";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createInterface } from "node:readline";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const [upstream, python, output] = process.argv.slice(2);
const load = (path) => import(pathToFileURL(resolve(upstream, path)));
const { clientScenarios, matchesSpecVersion } = await load("src/scenarios/index.ts");
const { wireSchemaErrors } = await load("src/validation/wire-schema.ts");
const { JsonRpcError } = await load("src/connection/index.ts");
const { DATED_SPEC_VERSIONS, DRAFT_PROTOCOL_VERSION } = await load("src/types.ts");

const SERVER = "scripts/run_local_mcp.py";
const SOURCES = [SERVER, "server_mcp.py", "src/verified_execution/runtime.py", "src/verified_execution/tools.py",
  "src/verified_execution/core.py", "config/local_tool.json"];
const VERSIONS = [...DATED_SPEC_VERSIONS, DRAFT_PROTOCOL_VERSION];
const SCENARIO_TIMEOUT_MS = 20000;
const CLIENT_INFO = { name: "vater-conformance", version: "1" };

function sha256(path) {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

async function open(specVersion, options, wireErrors) {
  const child = spawn(python, ["-I", SERVER], { stdio: ["pipe", "pipe", "ignore"] });
  const exited = once(child, "exit");
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  const notifications = [];
  const stateless = specVersion === DRAFT_PROTOCOL_VERSION;
  let id = 0;
  let initialized;

  const send = (frame) => {
    wireErrors.push(...wireSchemaErrors(specVersion, frame).map((error) => `request ${frame.method}: ${error}`));
    child.stdin.write(JSON.stringify(frame) + "\n");
  };
  const request = async (method, params = {}, extraHeaders) => {
    if (extraHeaders !== undefined) {
      throw new Error("per-request HTTP headers do not exist on standard input and output");
    }
    const frame = { jsonrpc: "2.0", id: ++id, method, params: stateless ? {
      ...params, _meta: {
        "io.modelcontextprotocol/protocolVersion": specVersion,
        "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
        "io.modelcontextprotocol/clientCapabilities": options?.capabilities ?? {},
        ...(params?._meta ?? {}),
      },
    } : params };
    send(frame);
    for (;;) {
      const line = await lines.next();
      if (line.done) throw new Error(`server closed its output during ${method}`);
      const message = JSON.parse(line.value);
      if (message.id === undefined) {
        notifications.push(message);
        continue;
      }
      wireErrors.push(...wireSchemaErrors(specVersion, message, method).map((error) => `response ${method}: ${error}`));
      if (message.id !== frame.id) throw new Error(`response id ${message.id} for request ${frame.id}`);
      if (message.error) throw new JsonRpcError(message.error.code, message.error.message, message.error.data);
      return message.result;
    }
  };
  if (!stateless) {
    initialized = await request("initialize", {
      protocolVersion: specVersion, capabilities: options?.capabilities ?? {},
      clientInfo: options?.clientInfo ?? CLIENT_INFO,
    });
    send({ jsonrpc: "2.0", method: "notifications/initialized" });
  }
  return {
    request,
    notifications,
    async discover() {
      if (!stateless) {
        return { capabilities: initialized.capabilities, serverInfo: initialized.serverInfo,
                 instructions: initialized.instructions, protocolVersion: initialized.protocolVersion };
      }
      return request("server/discover");
    },
    async close() {
      child.stdin.end();
      const timer = setTimeout(() => child.kill("SIGKILL"), 5000);
      await exited;
      clearTimeout(timer);
    },
  };
}

function withTimeout(promise, label) {
  let timer;
  return Promise.race([promise, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out after ${SCENARIO_TIMEOUT_MS} ms`)), SCENARIO_TIMEOUT_MS);
  })]).finally(() => clearTimeout(timer));
}

mkdirSync(output, { recursive: true });
const pkg = JSON.parse(readFileSync(resolve(upstream, "package.json"), "utf-8"));
const commit = execFileSync("git", ["-C", upstream, "rev-parse", "HEAD"], { encoding: "utf-8" }).trim();
const sourceSha256 = Object.fromEntries(SOURCES.map((path) => [path, sha256(path)]));
for (const specVersion of VERSIONS) {
  const scenarios = [];
  for (const scenario of clientScenarios.values()) {
    if (!matchesSpecVersion(scenario.source, specVersion)) continue;
    const connections = [];
    const wireErrors = [];
    let checks;
    try {
      checks = await withTimeout(scenario.run({
        serverUrl: "stdio://local-process",
        specVersion,
        connect: async (options) => {
          const connection = await open(specVersion, options, wireErrors);
          connections.push(connection);
          return connection;
        },
      }), scenario.name);
    } catch (error) {
      checks = [{ id: scenario.name, status: "ERROR", errorMessage: String(error?.message ?? error) }];
    } finally {
      await Promise.all(connections.map((connection) => connection.close().catch(() => {})));
    }
    scenarios.push({
      name: scenario.name,
      checks: checks.map(({ id, status, errorMessage }) => ({ id, status, ...(errorMessage ? { errorMessage } : {}) })),
      wireErrors,
    });
  }
  const record = {
    specVersion,
    upstream: { package: pkg.name, version: pkg.version, commit },
    server: SERVER,
    sourceSha256,
    scenarios,
  };
  writeFileSync(resolve(output, `${specVersion}.json`), JSON.stringify(record, null, 2) + "\n");
  const passed = scenarios.filter((s) => s.checks.length && s.checks.every((c) => ["SUCCESS", "INFO"].includes(c.status)));
  console.log(`${specVersion}: ${scenarios.length} scenarios, ${passed.length} passed`);
}
