// task.optional_webmcp: WebMCP tool registration prototype for Chrome 158's document.modelContext.
//
// The page registers only the tools in TOOLS. It validates every argument itself, because the
// browser passes arguments to execute() without checking inputSchema (observed in Chrome 158).
// Tools that run on the host are decided again by the Python host, which ignores whatever the
// page claims about its authority. Every execution is shown with who started it: the user
// (a trusted click) or an AI agent (a WebMCP call). Nothing here is reachable from window.

const U32_MAX = 4294967295;
const ADD_SCHEMA = Object.freeze({
  type: "object",
  properties: {
    left: { type: "integer", minimum: 0, maximum: U32_MAX },
    right: { type: "integer", minimum: 0, maximum: U32_MAX },
  },
  required: ["left", "right"],
  additionalProperties: false,
});

// 002: which operations stay in the browser and which need the host (an external effect or the core).
export const TOOLS = Object.freeze({
  add_numbers: Object.freeze({
    runsIn: "page",
    title: "Add two numbers in the page",
    description: "Adds two unsigned 32-bit integers in this page. Nothing leaves the browser.",
    inputSchema: ADD_SCHEMA,
    annotations: { readOnlyHint: true },
  }),
  checked_add: Object.freeze({
    runsIn: "host",
    title: "Add two numbers on the host",
    description: "Adds two unsigned 32-bit integers in the Python common core, which checks the input again.",
    inputSchema: ADD_SCHEMA,
    annotations: { readOnlyHint: true },
  }),
  send_report: Object.freeze({
    runsIn: "host",
    title: "Send a report",
    description: "Sends a report outside this page. An operator approves each report on the host; this page cannot.",
    inputSchema: Object.freeze({
      type: "object",
      properties: {
        title: { type: "string", minLength: 1, maxLength: 120 },
        body: { type: "string", minLength: 1, maxLength: 2000 },
      },
      required: ["title", "body"],
      additionalProperties: false,
    }),
    annotations: { readOnlyHint: false, consequentialHint: true },
  }),
});

// 004: the page's own check of types, ranges, lengths and fields.
export function validate(schema, args) {
  if (args === null || typeof args !== "object" || Array.isArray(args)) return ["arguments must be an object"];
  const problems = [];
  for (const key of Object.keys(args)) {
    if (!Object.hasOwn(schema.properties, key)) problems.push(`unexpected field: ${key}`);
  }
  for (const key of schema.required) {
    if (!Object.hasOwn(args, key)) problems.push(`missing field: ${key}`);
  }
  for (const [key, rule] of Object.entries(schema.properties)) {
    if (!Object.hasOwn(args, key)) continue;
    const value = args[key];
    if (rule.type === "integer") {
      if (typeof value !== "number" || !Number.isInteger(value)) problems.push(`${key}: an integer is required`);
      else if (value < rule.minimum || value > rule.maximum) {
        problems.push(`${key}: outside [${rule.minimum}, ${rule.maximum}]`);
      }
    } else if (rule.type === "string") {
      if (typeof value !== "string") problems.push(`${key}: a string is required`);
      else if (value.length < rule.minLength || value.length > rule.maxLength) {
        problems.push(`${key}: length outside [${rule.minLength}, ${rule.maxLength}]`);
      }
    }
  }
  return problems;
}

// 001 / 009: identify an environment without WebMCP; the page then works as an ordinary page.
export function detectWebMCP(scope = globalThis) {
  if (!scope.isSecureContext) return { supported: false, reason: "insecure_context" };
  const context = scope.document && scope.document.modelContext;
  if (!context) return { supported: false, reason: "no_model_context" };
  if (typeof context.registerTool !== "function") return { supported: false, reason: "unsupported_api" };
  return { supported: true, reason: "document.modelContext" };
}

function addInPage(args) {
  const sum = args.left + args.right;
  return sum <= U32_MAX ? { ok: true, status: "completed", value: sum }
    : { ok: false, status: "refused", error_code: "OVERFLOW" };
}

// 006: only the arguments are sent; the session is an HttpOnly cookie the page cannot read.
async function callHost(name, args) {
  try {
    const response = await fetch(`/api/tools/${encodeURIComponent(name)}`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ arguments: args }),
    });
    return await response.json();
  } catch (error) {
    return { ok: false, status: "host_unreachable" };
  }
}

const SOURCES = { user: "利用者", agent: "AI エージェント" };
const STATUS_TEXT = {
  completed: "完了", refused: "拒否", invalid_arguments: "入力不正", approval_required:
  "承認待ち（ホストで運用者が承認するまで送信されません）", sent: "送信済み", not_granted: "権限なし",
  host_unreachable: "ホストに接続できません", unknown_tool: "未登録のツール",
};

// 007: every execution is listed with who started it.
function logActivity(source, name, outcome) {
  const list = document.getElementById("activity");
  if (!list) return;
  const entry = document.createElement("li");
  entry.dataset.source = source;
  entry.dataset.tool = name;
  entry.dataset.status = outcome.status;
  const title = Object.hasOwn(TOOLS, name) ? TOOLS[name].title : name;
  const value = outcome.value !== undefined ? ` = ${outcome.value}`
    : outcome.result && outcome.result.value !== undefined && outcome.result.value !== null ? ` = ${outcome.result.value}` : "";
  entry.textContent = `${SOURCES[source]}: ${title} → ${STATUS_TEXT[outcome.status] ?? outcome.status}${value}`;
  list.append(entry);
}

async function run(name, args, source) {
  let outcome;
  if (!Object.hasOwn(TOOLS, name)) {
    outcome = { ok: false, status: "unknown_tool" };
  } else {
    const problems = validate(TOOLS[name].inputSchema, args);
    if (problems.length) outcome = { ok: false, status: "invalid_arguments", problems };
    else if (TOOLS[name].runsIn === "page") outcome = addInPage(args);
    else outcome = await callHost(name, args);
  }
  logActivity(source, name, outcome);
  return outcome;
}

const registrations = new AbortController(); // module-private: no other script can unregister the tools

async function registerAll(context) {
  for (const [name, tool] of Object.entries(TOOLS)) {
    await context.registerTool({
      name,
      title: tool.title,
      description: tool.description,
      inputSchema: tool.inputSchema,
      annotations: tool.annotations,
      async execute(args) {
        const outcome = await run(name, args, "agent");
        return { content: [{ type: "text", text: JSON.stringify(outcome) }], isError: !outcome.ok };
      },
    }, { signal: registrations.signal });
  }
}

function numberField(id) {
  const text = document.getElementById(id).value.trim();
  return /^\d+$/.test(text) ? Number(text) : text;
}

function onTrustedClick(id, handler) {
  document.getElementById(id).addEventListener("click", (event) => {
    if (!event.isTrusted) return; // a script-made click is not the user
    handler();
  });
}

async function init() {
  const status = document.getElementById("webmcp-status");
  const detected = detectWebMCP();
  status.dataset.supported = String(detected.supported);
  status.dataset.reason = detected.reason;
  if (detected.supported) {
    try {
      await registerAll(document.modelContext);
      status.textContent = "WebMCP: 有効。AI エージェントがこのページのツールを呼べます。";
    } catch (error) {
      status.dataset.supported = "false";
      status.dataset.reason = "registration_failed";
      status.textContent = "WebMCP: 登録できませんでした。通常の画面で操作できます。";
    }
  } else {
    status.textContent = `WebMCP: 利用できません（${detected.reason}）。通常の画面で操作できます。`;
  }
  const show = (id, outcome) => {
    document.getElementById(id).textContent = JSON.stringify(outcome);
  };
  onTrustedClick("add-page", async () => show("add-result",
    await run("add_numbers", { left: numberField("left"), right: numberField("right") }, "user")));
  onTrustedClick("add-host", async () => show("add-result",
    await run("checked_add", { left: numberField("left"), right: numberField("right") }, "user")));
  onTrustedClick("send-report", async () => show("report-result", await run("send_report", {
    title: document.getElementById("report-title").value, body: document.getElementById("report-body").value,
  }, "user")));
  document.body.dataset.ready = "true";
}

init();
