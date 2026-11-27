"""task.testing.023: the HTTP interface matches its own OpenAPI document.

HTTP is an adopted optional profile (docs/RELEASE_PROFILES.json, with_http),
so the contract is tested rather than marked not applicable. The document is
fetched from the running app, never rebuilt here. Then:

* the app's routes and the documented paths and methods match;
* every documented (path, method, status) is produced by a real request, and
  every observed status is documented;
* every response body validates against the schema documented for its status;
* the documented request schema decides acceptance: a body it rejects is 400;
  a body it accepts is queued (202), except the stricter decimal-notation
  rule the schema's own description announces.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import threading

from jsonschema import Draft202012Validator
import pytest
from starlette.routing import Route

from adapter_drivers import TOKEN
from test_contract_inventory import request_violations
from verified_execution.arithmetic import checked_multiply_spec
from verified_execution.core import LocalAuthority
from verified_execution.http_local import MAX_BYTES, MAX_JOBS, create_app
from verified_execution.tools import ToolRegistry, default_registry, thaw

GRANT = LocalAuthority(frozenset({"checked_add", "checked_multiply"}))
REGISTRY = default_registry().with_tools(checked_multiply_spec())
VALID = {"checked_add": {"left": 1, "right": 2}, "checked_multiply": {"left": 2, "right": 3}}


async def call(app, method, path, body=b"", *, token=TOKEN, content_type="application/json", disconnect=False):
    """One in-process ASGI exchange with full control over headers and the body stream."""
    messages = [{"type": "http.request", "body": body, "more_body": disconnect}]
    sent = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    headers = [(b"content-length", str(len(body)).encode())]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    if content_type is not None:
        headers.append((b"content-type", content_type.encode()))
    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
               "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
               "server": ("127.0.0.1", 80), "client": ("127.0.0.1", 1), "headers": headers}, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    try:
        return status, json.loads(payload) if payload else None
    except ValueError:
        return status, payload.decode(errors="replace")


def document(app):
    status, spec = asyncio.run(call(app, "GET", "/openapi.json"))
    assert status == 200
    return spec


def template(path, spec):
    return path if path in spec["paths"] else "/jobs/{job_id}"


class Recorder:
    """Checks each response against the document and remembers what was produced."""

    def __init__(self, spec):
        self.spec, self.seen = spec, set()

    def check(self, method, path, status, body):
        operation = self.spec["paths"][template(path, self.spec)][method.lower()]
        assert str(status) in operation["responses"], (method, path, status, body)
        schema = operation["responses"][str(status)]["content"]["application/json"]["schema"]
        errors = [error.message for error in Draft202012Validator(schema).iter_errors(body)]
        assert errors == [], (method, path, status, body, errors)
        self.seen.add((template(path, self.spec), method.lower(), str(status)))
        return body

    def documented(self):
        return {(path, method, status) for path, operations in self.spec["paths"].items()
                for method, operation in operations.items() for status in operation["responses"]}


def test_routes_match_the_document():
    app = create_app(TOKEN, GRANT, registry=REGISTRY)
    spec = document(app)
    concrete = {f"/tools/{name}/jobs" for name in VALID} | {"/jobs", "/jobs/{job_id}"}
    assert set(spec["paths"]) == concrete
    routes = {(route.path, method.lower()) for route in app.routes if isinstance(route, Route)
              for method in route.methods - {"HEAD"}}
    documented = {(path if not path.startswith("/tools/") else "/tools/{tool_name}/jobs", method)
                  for path, operations in spec["paths"].items() for method in operations}
    # The one undocumented route is the document itself.
    assert routes - documented == {("/openapi.json", "get")} and documented <= routes
    for path, operations in spec["paths"].items():
        for operation in operations.values():
            assert all("schema" in response["content"]["application/json"]
                       for response in operation["responses"].values())
            if path.startswith("/tools/"):
                name = path.split("/")[2]
                assert operation["requestBody"]["content"]["application/json"]["schema"] \
                    == thaw(REGISTRY.get(name).input_schema)


def test_every_documented_response_is_produced_and_matches_its_schema():
    release = threading.Event()
    blocking = replace(default_registry().get("checked_add"),
                       run=lambda operands: release.wait(10) and sum(operands))
    app = create_app(TOKEN, GRANT, registry=ToolRegistry([blocking, checked_multiply_spec()]))
    denied = create_app(TOKEN, LocalAuthority(frozenset()), registry=REGISTRY)
    recorder = Recorder(document(app))

    async def scenario():
        async def expect(method, path, status, *args, target=app, **kwargs):
            observed, body = await call(target, method, path, *args, **kwargs)
            assert observed == status, (method, path, observed, body)
            return recorder.check(method, path, observed, body)

        async def wait_for(job, states):
            for _ in range(500):
                _, body = await call(app, "GET", job)
                if body["state"] in states:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError(f"{job} never reached {states}")

        for path in ("/jobs", "/tools/checked_add/jobs", "/tools/checked_multiply/jobs"):
            valid = json.dumps(VALID["checked_multiply" if "multiply" in path else "checked_add"]).encode()
            await expect("POST", path, 401, valid, token=None)
            await expect("POST", path, 401, valid, token="someoneElsesCredential" * 2)
            await expect("POST", path, 403, valid, target=denied)
            await expect("POST", path, 415, valid, content_type="text/plain")
            await expect("POST", path, 413, b"[" + b" " * MAX_BYTES + b"]")
            await expect("POST", path, 400, b'{"left": 1,')
            await expect("POST", path, 400, b'{"left": 1}')
            await expect("POST", path, 400, b'{"left": 1,', disconnect=True)
        # checked_add blocks until released: one job runs, the rest stay prepared.
        started = await expect("POST", "/jobs", 202, b'{"left": 1, "right": 2}')
        running = "/jobs/" + started["job_id"]
        await wait_for(running, {"running"})
        await expect("GET", running, 200)
        queued = []
        for _ in range(MAX_JOBS - 1):
            accepted = await expect("POST", "/tools/checked_add/jobs", 202, b'{"left": 2, "right": 2}')
            queued.append("/jobs/" + accepted["job_id"])
        await expect("GET", queued[0], 200)
        await expect("POST", "/tools/checked_multiply/jobs", 429, b'{"left": 2, "right": 3}')
        await expect("POST", "/jobs", 429, b'{"left": 2, "right": 3}')
        await expect("POST", "/tools/checked_add/jobs", 429, b'{"left": 2, "right": 3}')
        await expect("DELETE", queued[0], 200)
        await expect("GET", queued[0], 200)        # cancelled
        await expect("DELETE", running, 409)        # running
        for method in ("GET", "DELETE"):
            await expect(method, running, 401, token=None)
            await expect(method, "/jobs/" + "0" * 32, 404)
        release.set()
        for job in queued[1:] + [running]:
            await wait_for(job, {"succeeded"})
        await expect("GET", running, 200)          # succeeded
        await expect("DELETE", running, 409)        # finished
        accepted = await expect("POST", "/tools/checked_multiply/jobs", 202, b'{"left": 4294967295, "right": 2}')
        failed = "/jobs/" + accepted["job_id"]
        await wait_for(failed, {"failed"})
        assert (await expect("GET", failed, 200))["error"] == "OVERFLOW"

    try:
        asyncio.run(scenario())
    finally:
        release.set()
    assert recorder.seen == recorder.documented()


@pytest.mark.parametrize("name", sorted(VALID))
def test_the_documented_request_schema_decides_acceptance(name):
    app = create_app(TOKEN, GRANT, registry=REGISTRY)
    schema = document(app)["paths"][f"/tools/{name}/jobs"]["post"]["requestBody"]["content"][
        "application/json"]["schema"]
    validator = Draft202012Validator(schema)
    bodies = [payload for _label, payload in request_violations(name)] + [
        VALID[name], {"left": 0, "right": 0}, {"left": 4294967295, "right": 0}, {"right": 7, "left": 9}]
    for payload in bodies:
        status, reply = asyncio.run(call(app, "POST", f"/tools/{name}/jobs", json.dumps(payload).encode()))
        assert status == (202 if validator.is_valid(payload) else 400), (payload, status, reply)
    # The schema's own description announces the one stricter rule: decimal notation.
    assert "1.0" in schema["description"]
    status, _ = asyncio.run(call(app, "POST", f"/tools/{name}/jobs", b'{"left": 1.0, "right": 2}'))
    assert validator.is_valid({"left": 1.0, "right": 2}) and status == 400


def test_names_and_methods_outside_the_document():
    app = create_app(TOKEN, GRANT, registry=REGISTRY)
    assert asyncio.run(call(app, "POST", "/tools/delete_file/jobs", b"{}")) == (404, {"error": "UNKNOWN_TOOL"})
    for method, path in (("PUT", "/jobs"), ("GET", "/jobs"), ("POST", "/jobs/" + "0" * 32),
                         ("DELETE", "/tools/checked_add/jobs")):
        assert asyncio.run(call(app, method, path))[0] == 405
