"""Authenticated loopback HTTP diagnostics for read-only registry tools."""

import asyncio
from dataclasses import dataclass
from typing import Iterable
from hmac import compare_digest
import json
import os
from uuid import uuid4

from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .core import LocalAuthority, OperationError
from .json_bounds import check_json_depth
from .runtime import ToolRuntime
from .tools import ToolRegistry, ToolSpec, thaw

LOOPBACK = "127.0.0.1"
MAX_BYTES = 4096
MAX_DEPTH = 32
MAX_JOBS = 64
DEFAULT_TOOL = "checked_add"
_DEFAULT = object()


@dataclass
class Job:
    state: str = "prepared"
    result: int | None = None
    error: str | None = None


def response(status: int, **payload) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(payload, status_code=status, headers=headers)


def create_app(token: str, authority: LocalAuthority | None = None, *,
               registry: ToolRegistry | None = None, tools: Iterable[str] | None = None,
               default_tool: str | None | object = _DEFAULT) -> Starlette:
    """The host's single credential identifies the local operator.

    ADAPTER-001: schemas, argument validation, authorization and result checks
    come from the shared ToolRuntime. This module owns only HTTP concerns:
    bearer token, media type, body size, JSON framing and the job lifecycle.

    tools: names the host serves; None serves every read-only registry tool.
    Each served tool gets POST /tools/{name}/jobs. POST /jobs remains the
    version-1 route and runs default_tool (checked_add when served).
    """
    if not isinstance(token, str) or len(token) < 32 or not token.isascii() or not token.isalnum():
        raise ValueError("a host-configured alphanumeric bearer token of at least 32 characters is required")
    header = f"Bearer {token}".encode("ascii")
    service = ToolRuntime(authority if authority is not None else LocalAuthority.calculator_only(), registry)
    executable = {spec.name: spec for spec in service.tools()}
    names = list(executable) if tools is None else list(tools)
    if not names or len(set(names)) != len(names) or any(name not in executable for name in names):
        raise ValueError("HTTP tools must be distinct registered read-only tools")
    served = {name: executable[name] for name in names}
    if default_tool is _DEFAULT:
        default_tool = DEFAULT_TOOL if DEFAULT_TOOL in served else None
    if default_tool is not None and default_tool not in served:
        raise ValueError("the default HTTP tool must be one of the served read-only tools")
    jobs: dict[str, Job] = {}
    background: set[asyncio.Task] = set()
    execution_slot = asyncio.Lock()

    def authorized(request: Request) -> bool:
        # task.http_interface.009: the trusted host token establishes the operator.
        supplied = request.headers.getlist("authorization")
        return len(supplied) == 1 and compare_digest(supplied[0].encode("latin-1"), header)

    async def run_job(job: Job, spec: ToolSpec, arguments: dict) -> None:
        async with execution_slot:
            if job.state == "cancelled":
                return
            # task.http_interface.005 / .015: a disconnected client cannot restart a job.
            # prepared -> running -> succeeded or failed. Only prepared -> cancelled is allowed.
            job.state = "running"
            try:
                outcome = await asyncio.to_thread(service.execute, spec.name, arguments)
                outcome = service.release(spec.name, arguments, outcome)
                job.result, job.error = outcome.result, outcome.error_code
            except Exception:
                job.error = "EXECUTION_FAILED"
            job.state = "succeeded" if job.error is None else "failed"

    async def start_named(request: Request) -> JSONResponse:
        if not authorized(request):
            return response(401, error="UNAUTHORIZED")
        spec = served.get(request.path_params["tool_name"])
        if spec is None:
            return response(404, error="UNKNOWN_TOOL")
        return await submit(request, spec)

    async def start(request: Request) -> JSONResponse:
        # Version-1 route: the host-chosen default tool.
        if not authorized(request):
            return response(401, error="UNAUTHORIZED")
        return await submit(request, served[default_tool])

    async def submit(request: Request, spec: ToolSpec) -> JSONResponse:
        if not service.permits(spec.name):
            return response(403, error="NOT_AUTHORIZED")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return response(415, error="UNSUPPORTED_MEDIA_TYPE")
        body = bytearray()
        try:
            async with asyncio.timeout(2):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_BYTES:
                        return response(413, error="BODY_TOO_LARGE")
                    body.extend(chunk)
        except (ClientDisconnect, TimeoutError):
            return response(400, error="INCOMPLETE_BODY")

        def unique_pairs(pairs):
            fields = dict(pairs)
            if len(fields) != len(pairs):
                raise ValueError("duplicate field")
            return fields

        try:
            raw = body.decode("utf-8")
            check_json_depth(raw, MAX_DEPTH)
            arguments = json.loads(raw, object_pairs_hook=unique_pairs)
        except ValueError:
            return response(400, error="INVALID_BODY")
        try:
            service.check(spec.name, arguments)
        except OperationError:
            return response(400, error="INVALID_BODY")
        if len(jobs) >= MAX_JOBS:
            oldest = next((key for key, job in jobs.items() if job.state not in {"prepared", "running"}), None)
            if oldest is None:
                return response(429, error="JOB_LIMIT")
            del jobs[oldest]
        identifier, job = uuid4().hex, Job()
        jobs[identifier] = job
        task = asyncio.create_task(run_job(job, spec, arguments))
        background.add(task)
        task.add_done_callback(background.discard)
        return response(202, job_id=identifier, state="prepared")

    async def status(request: Request) -> JSONResponse:
        if not authorized(request):
            return response(401, error="UNAUTHORIZED")
        job = jobs.get(request.path_params["job_id"])
        if job is None:
            return response(404, error="UNKNOWN_JOB")
        return response(200, state=job.state, result=job.result, error=job.error)

    async def cancel(request: Request) -> JSONResponse:
        if not authorized(request):
            return response(401, error="UNAUTHORIZED")
        job = jobs.get(request.path_params["job_id"])
        if job is None:
            return response(404, error="UNKNOWN_JOB")
        if job.state not in {"prepared", "cancelled"}:
            return response(409, error="CANNOT_CANCEL")
        job.state = "cancelled"
        return response(200, state=job.state)

    submit_codes = {202, 400, 401, 403, 413, 415, 429}

    def responses(codes: set[int]) -> dict:
        return {str(code): {"description": f"HTTP {code}"} for code in sorted(codes)}

    def submission(operation_id: str, spec: ToolSpec) -> dict:
        return {
            "operationId": operation_id,
            "summary": spec.description,
            "responses": responses(submit_codes),
            "requestBody": {"required": True,
                            "content": {"application/json": {"schema": thaw(spec.input_schema)}}},
        }

    job_parameter = [{"name": "job_id", "in": "path", "required": True, "schema": {"type": "string"}}]
    # OpenAPI lists one concrete path per served tool, each with its own schema.
    paths: dict = {f"/tools/{name}/jobs": {"post": submission(f"start_{name}", spec)}
                   for name, spec in served.items()}
    if default_tool is not None:
        paths["/jobs"] = {"post": submission("start", served[default_tool])}
    paths["/jobs/{job_id}"] = {
        "get": {"operationId": "status", "responses": responses({200, 401, 404}), "parameters": job_parameter},
        "delete": {"operationId": "cancel", "responses": responses({200, 401, 404, 409}),
                   "parameters": job_parameter},
    }
    routes = [Route("/tools/{tool_name}/jobs", start_named, methods=["POST"]),
              Route("/jobs/{job_id}", status, methods=["GET"]),
              Route("/jobs/{job_id}", cancel, methods=["DELETE"])]
    if default_tool is not None:
        routes.append(Route("/jobs", start, methods=["POST"]))

    async def openapi(request: Request) -> JSONResponse:
        if not authorized(request):
            return response(401, error="UNAUTHORIZED")
        return response(200, openapi="3.1.0", info={"title": "Vater local diagnostics", "version": "1"},
                        paths=paths, security=[{"localBearer": []}],
                        components={"securitySchemes": {"localBearer": {"type": "http", "scheme": "bearer"}}})

    return Starlette(routes=[*routes, Route("/openapi.json", openapi, methods=["GET"])])


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(os.environ.get("VATER_LOCAL_HTTP_TOKEN", "")),
                host=LOOPBACK, port=8765, access_log=False, timeout_keep_alive=2)


if __name__ == "__main__":
    main()
