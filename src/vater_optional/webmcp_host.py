"""Python host for the WebMCP page prototype (task.optional_webmcp.*).

Serves web/webmcp/ under a strict Content-Security-Policy and decides, on the host, every
operation the page forwards. The page's own authority is never trusted (005 / 006):

  - the page's session is an HttpOnly, SameSite=Strict cookie scoped to /api, so no script on
    the page can read it; the API also requires a JSON body and the page's own Origin;
  - the page principal may call only PAGE_GRANT; any claimed principal, authority or
    approval in the request body is ignored;
  - checked_add runs in the Python common core (ToolRuntime), which validates the arguments
    again with the tool's contract;
  - send_report is the one operation with an external effect. It runs only after an operator
    approves that exact request (principal, tool and canonical arguments) through
    /api/approvals with the operator credential, which the page never holds (008). An
    approval is used once. The effect is appended to an in-memory outbox; nothing is sent.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import socket
from threading import Thread
from time import monotonic, sleep
from typing import Iterator

WEB = Path(__file__).resolve().parents[2] / "web"
CSP = ("default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; img-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
PAGE_GRANT = frozenset({"checked_add", "send_report"})
CONSEQUENTIAL = frozenset({"send_report"})
MAX_BODY_BYTES = 4096
MAX_SESSIONS = 32
REPORT_LIMITS = {"title": 120, "body": 2000}


def _digest(principal: str, tool: str, arguments: dict) -> str:
    canonical = json.dumps([principal, tool, arguments], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Host:
    """The host-side state: page sessions, pending and granted approvals, and the outbox."""

    def __init__(self, operator_token: str) -> None:
        if len(operator_token) < 32:
            raise ValueError("the operator credential must be at least 32 characters")
        self.operator_token = operator_token
        self.sessions: list[str] = []
        self.pending: dict[str, dict] = {}
        self.approved: set[str] = set()
        self.outbox: list[dict] = []
        from verified_execution.core import CalculationService, LocalAuthority
        self.core = CalculationService(LocalAuthority.calculator_only())

    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self.sessions = [*self.sessions[-(MAX_SESSIONS - 1):], token]
        return token

    def page_principal(self, cookie: str | None) -> str | None:
        if not cookie:
            return None
        matched = False
        for token in self.sessions:  # constant-time comparison against every live session
            matched |= hmac.compare_digest(cookie.encode(), token.encode())
        return "page" if matched else None

    def call(self, principal: str, tool: str, arguments: object) -> tuple[int, dict]:
        if tool not in PAGE_GRANT:
            return 403, {"ok": False, "status": "not_granted"}
        if not isinstance(arguments, dict):
            return 400, {"ok": False, "status": "invalid_arguments"}
        if tool == "checked_add":
            result = self.core.execute("checked_add", arguments).to_dict()  # the common core re-validates
            return (200 if result.get("ok") else 400), {"ok": bool(result.get("ok")), "status": "completed"
                                                         if result.get("ok") else "refused", "result": result}
        if set(arguments) != set(REPORT_LIMITS) or not all(
                isinstance(arguments[key], str) and 0 < len(arguments[key]) <= limit
                for key, limit in REPORT_LIMITS.items()):
            return 400, {"ok": False, "status": "invalid_arguments"}
        digest = _digest(principal, tool, arguments)
        if digest not in self.approved:
            self.pending[digest] = {"principal": principal, "tool": tool, "arguments": arguments}
            return 202, {"ok": False, "status": "approval_required", "request": digest}
        self.approved.discard(digest)  # an approval is used once
        self.pending.pop(digest, None)
        self.outbox.append({"tool": tool, "arguments": arguments, "request": digest})
        return 200, {"ok": True, "status": "sent", "request": digest}

    def approve(self, offered: str | None, digest: str) -> tuple[int, dict]:
        if not offered or not hmac.compare_digest(offered.encode(), self.operator_token.encode()):
            return 403, {"ok": False, "status": "operator_credential_required"}
        if digest not in self.pending:
            return 404, {"ok": False, "status": "unknown_request"}
        self.approved.add(digest)
        return 200, {"ok": True, "status": "approved", "request": digest}


def create_app(host: Host, directory: str = "webmcp"):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    root = WEB / directory
    security = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer"}

    async def index(request: Request) -> Response:
        response = Response((root / "index.html").read_bytes(), media_type="text/html; charset=utf-8",
                            headers=security)
        response.set_cookie("vater_page", host.new_session(), httponly=True, samesite="strict", path="/api")
        return response

    async def asset(request: Request) -> Response:
        name = request.path_params["name"]
        kinds = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".wgsl": "text/plain; charset=utf-8"}
        path = (root / name).resolve()
        if path.parent != root.resolve() or path.suffix not in kinds or not path.is_file():
            return Response(status_code=404, headers=security)
        return Response(path.read_bytes(), media_type=kinds[path.suffix], headers=security)

    def same_origin(request: Request) -> bool:
        origin = request.headers.get("origin")
        return origin is not None and origin == f"{request.url.scheme}://{request.url.netloc}"

    async def tool(request: Request) -> JSONResponse:
        principal = host.page_principal(request.cookies.get("vater_page"))
        if principal is None:
            return JSONResponse({"ok": False, "status": "no_page_session"}, status_code=401, headers=security)
        if not same_origin(request) or request.headers.get("content-type", "").split(";")[0] != "application/json":
            return JSONResponse({"ok": False, "status": "cross_origin_or_not_json"}, status_code=403,
                                headers=security)
        body = await request.body()
        try:
            if len(body) > MAX_BODY_BYTES:
                raise ValueError
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            return JSONResponse({"ok": False, "status": "invalid_body"}, status_code=400, headers=security)
        # Only "arguments" is read; a claimed principal, authority or approval is ignored.
        status, answer = host.call(principal, request.path_params["name"], payload.get("arguments"))
        return JSONResponse(answer, status_code=status, headers=security)

    async def approval(request: Request) -> JSONResponse:
        header = request.headers.get("authorization", "")
        offered = header[len("Bearer "):] if header.startswith("Bearer ") else None
        status, answer = host.approve(offered, request.path_params["digest"])
        return JSONResponse(answer, status_code=status, headers=security)

    return Starlette(routes=[Route("/", index), Route("/api/tools/{name}", tool, methods=["POST"]),
                             Route("/api/approvals/{digest}", approval, methods=["POST"]),
                             Route("/{name}", asset)])


@contextmanager
def serve(app, host: str = "127.0.0.1") -> Iterator[int]:
    """Serve an ASGI app on a free loopback port for the duration of the block."""
    import uvicorn

    sock = socket.socket()
    sock.bind((host, 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = monotonic() + 5
    while not server.started and monotonic() < deadline:
        sleep(0.01)
    try:
        yield sock.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
