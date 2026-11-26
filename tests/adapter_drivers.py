"""Drive every protocol adapter with the same call and normalize the outcome.

Each driver returns an Outcome so a single test vector can be checked
against the shared runtime, the MCP adapter and the HTTP adapter.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from importlib import util
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "testOnlyCredential" * 3


@dataclass(frozen=True)
class Outcome:
    ok: bool
    value: int | None
    error_code: str | None
    # "execution": the call ran (or was refused) inside the runtime.
    # "acceptance": the adapter refused the request before queueing it.
    stage: str = "execution"


def runtime_outcome(runtime, name: str, arguments: Any) -> tuple[Outcome, tuple[str, ...]]:
    result = runtime.release(name, arguments, runtime.execute(name, arguments))
    return Outcome(result.succeeded, result.result, result.error_code), result.states


# --- MCP ------------------------------------------------------------------

def load_mcp_adapter(module_name: str = "server_mcp_driver"):
    spec = util.spec_from_file_location(module_name, ROOT / "server_mcp.py")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def with_sdk_client(server, scenario):
    """Real official SDK client, in-process dispatch (no wire framing)."""
    from mcp import Client

    async def run():
        async with Client(server, mode="2026-07-28") as client:
            return await scenario(client)
    return asyncio.run(run())


def mcp_outcome(server, name: str, arguments: Any) -> Outcome:
    async def scenario(client):
        return await client.call_tool(name, arguments)

    reply = with_sdk_client(server, scenario)
    if reply.is_error:
        return Outcome(False, None, reply.content[0].text)
    return Outcome(True, reply.structured_content["result"], None)


# --- HTTP -----------------------------------------------------------------

async def asgi(app, method: str, path: str, body: bytes = b"") -> tuple[int, Any]:
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    sent = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
               "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
               "root_path": "", "server": ("127.0.0.1", 80), "client": ("127.0.0.1", 1),
               "headers": [(b"authorization", f"Bearer {TOKEN}".encode()),
                           (b"content-type", b"application/json"),
                           (b"content-length", str(len(body)).encode())]}, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(payload)


def http_outcome(app, arguments: Any, tool: str | None = None) -> Outcome:
    """POST to /tools/{tool}/jobs, or to the version-1 /jobs route without a tool."""
    path = "/jobs" if tool is None else f"/tools/{tool}/jobs"

    async def scenario():
        status, started = await asgi(app, "POST", path, json.dumps(arguments).encode())
        if status != 202:
            return Outcome(False, None, started["error"], stage="acceptance")
        for _ in range(500):
            _, job = await asgi(app, "GET", f"/jobs/{started['job_id']}")
            if job["state"] not in {"prepared", "running"}:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("HTTP job did not finish")
        return Outcome(job["state"] == "succeeded", job["result"], job["error"])

    return asyncio.run(scenario())
