"""Authenticated loopback HTTP diagnostics for the read-only calculator."""

import asyncio
from dataclasses import dataclass
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
from .tools import ToolRegistry, thaw

LOOPBACK = "127.0.0.1"
MAX_BYTES = 4096
MAX_DEPTH = 32
MAX_JOBS = 64
DEFAULT_TOOL = "checked_add"


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
               registry: ToolRegistry | None = None, tool: str = DEFAULT_TOOL) -> Starlette:
    """The host's single credential identifies the local operator.

    ADAPTER-001: schemas, argument validation, authorization and result checks
    come from the shared ToolRuntime. This module owns only HTTP concerns:
    bearer token, media type, body size, JSON framing and the job lifecycle.
    """
    if not isinstance(token, str) or len(token) < 32 or not token.isascii() or not token.isalnum():
        raise ValueError("a host-configured alphanumeric bearer token of at least 32 characters is required")
    header = f"Bearer {token}".encode("ascii")
    service = ToolRuntime(authority if authority is not None else LocalAuthority.calculator_only(), registry)
    spec = next((candidate for candidate in service.tools() if candidate.name == tool), None)
    if spec is None:
        raise ValueError("the HTTP tool must be a registered read-only tool")
    input_schema = thaw(spec.input_schema)
    jobs: dict[str, Job] = {}
    background: set[asyncio.Task] = set()
    execution_slot = asyncio.Lock()

    def authorized(request: Request) -> bool:
        # task.http_interface.009: the trusted host token establishes the operator.
        supplied = request.headers.getlist("authorization")
        return len(supplied) == 1 and compare_digest(supplied[0].encode("latin-1"), header)

    async def run_job(job: Job, arguments: dict) -> None:
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

    async def start(request: Request) -> JSONResponse:
        if not authorized(request):
            return response(401, error="UNAUTHORIZED")
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
        task = asyncio.create_task(run_job(job, arguments))
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

    operations = (
        ("/jobs", "POST", start, {202, 400, 401, 403, 413, 415, 429}),
        ("/jobs/{job_id}", "GET", status, {200, 401, 404}),
        ("/jobs/{job_id}", "DELETE", cancel, {200, 401, 404, 409}),
    )
    paths: dict = {}
    for path, method, endpoint, codes in operations:
        description = {
            "operationId": endpoint.__name__,
            "responses": {str(code): {"description": f"HTTP {code}"} for code in sorted(codes)},
        }
        if method == "POST":
            description["requestBody"] = {
                "required": True, "content": {"application/json": {"schema": input_schema}}
            }
        else:
            description["parameters"] = [
                {"name": "job_id", "in": "path", "required": True, "schema": {"type": "string"}}
            ]
        paths.setdefault(path, {})[method.lower()] = description

    async def openapi(request: Request) -> JSONResponse:
        if not authorized(request):
            return response(401, error="UNAUTHORIZED")
        return response(200, openapi="3.1.0", info={"title": "Vater local diagnostics", "version": "1"},
                        paths=paths, security=[{"localBearer": []}],
                        components={"securitySchemes": {"localBearer": {"type": "http", "scheme": "bearer"}}})

    return Starlette(routes=[
        *(Route(path, endpoint, methods=[method]) for path, method, endpoint, _ in operations),
        Route("/openapi.json", openapi, methods=["GET"]),
    ])


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(os.environ.get("VATER_LOCAL_HTTP_TOKEN", "")),
                host=LOOPBACK, port=8765, access_log=False, timeout_keep_alive=2)


if __name__ == "__main__":
    main()
