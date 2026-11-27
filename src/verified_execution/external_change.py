"""Ledger-backed changes in an external system. First destination: GitHub Issues.

The rules come from contracts/github_issue_destination.json.

STATE MACHINE (one operation, one request_key):
  reserve            -> prepared, durable, needs a host approval
  begin_dispatch     -> dispatched, durable, committed BEFORE the request
  send:
    confirmed        -> target check -> succeeded, result and version stored
    not executed     -> (rate limit) resend the same request, bounded
    rejected         -> failed
    no reply, 5xx    -> look up -> exactly one match -> succeeded (reconciled)
                                -> otherwise -> unknown, human reconciliation
  replay of the key  -> stored result, or look up only; never a blind resend

Machine contracts:
  EXTERNAL-011: the dispatched state is durable before any request leaves.
  EXTERNAL-012: every confirmed result is stored and readable by operation id.
  EXTERNAL-015: without destination idempotency, a create that may have run is
                never sent again.
  EXTERNAL-016: an unresolved outcome goes to lookup, then to a person.
  EXTERNAL-017: resend only after a reply proving the request did not execute.
  EXTERNAL-021: the destination's object version is stored with the result,
                and an object that does not match the request is refused.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Mapping, NamedTuple, Protocol
from urllib.parse import urlencode
import urllib.request

from .core import LocalAuthority, OperationError
from .durable import validate_request_key
from .ledger import LedgerError, Operation, OperationLedger
from .recorded_tool_bridge import BridgeResult, declared_failures, within_contract
from .tools import DEFAULT_CONTRACTS_DIR, ToolSpec

TOOL_NAME = "github_test_create_issue"
EVIDENCE_LABEL = "vater-external-evidence"
MAX_TITLE_CHARACTERS = 120
LOOKUP_MARGIN_MS = 300_000
MAX_LOOKUP_PAGES = 10


# --- transport --------------------------------------------------------------

@dataclass(frozen=True)
class HttpReply:
    status: int
    headers: Mapping[str, str]  # lower-case names
    body: Any


class TransportError(Exception):
    """No reply arrived: the request may or may not have executed."""


class Transport(Protocol):
    def request(self, method: str, path: str, *, json_body: Any = None,
                query: Mapping[str, Any] | None = None) -> HttpReply: ...


class UrllibTransport:
    """GitHub REST over HTTPS with the standard library. Paths are repo-relative."""

    def __init__(self, repository: str, token: str, *, base_url: str = "https://api.github.com",
                 timeout: float = 20.0) -> None:
        if repository.count("/") != 1 or not token:
            raise ValueError("repository must be owner/name and a token is required")
        self._prefix = f"{base_url}/repos/{repository}"
        self._token = token
        self._timeout = timeout

    def request(self, method, path, *, json_body=None, query=None):
        import urllib.error
        import urllib.request
        url = self._prefix + path + ("?" + urlencode(query) if query else "")
        data = None if json_body is None else json.dumps(json_body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "vater-tool-execution-runtime",
            **({"Content-Type": "application/json"} if data is not None else {}),
        })
        import http.client
        # Any failure while sending OR while reading a reply (including a body cut
        # off mid-read, http.client.IncompleteRead) means no usable reply arrived:
        # the request may or may not have executed.
        unreadable = (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException)
        try:
            with _OPENER.open(request, timeout=self._timeout) as response:
                return HttpReply(response.status, _lower(response.headers), _json(response.read()))
        except urllib.error.HTTPError as error:
            try:
                return HttpReply(error.code, _lower(error.headers), _json(error.read()))
            except unreadable as read_error:
                raise TransportError(type(read_error).__name__) from None
        except unreadable as error:
            raise TransportError(type(error).__name__) from None


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """urllib copies every request header, Authorization included, onto the
    redirected request, even when it goes to another host. Redirects are never
    followed: the 3xx reply itself is returned (a definitive non-execution for a
    create), and the credential never leaves the configured host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_RefuseRedirects())


def _lower(headers) -> dict[str, str]:
    return {name.lower(): value for name, value in headers.items()}


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


# --- result store (separate table; the formally checked ledger is unchanged) -

class ExternalResultStore:
    """Per-operation destination results, readable by operation id (EXTERNAL-012)."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with self._open() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS external_results (
                    operation_id TEXT PRIMARY KEY,
                    destination TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN
                        ('dispatched','succeeded','reconciled','failed','human_required')),
                    object_id TEXT,
                    version_json TEXT,
                    detail TEXT,
                    dispatched_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )""")

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def dispatched(self, operation_id: str, destination: str, at_ms: int) -> None:
        with self._open() as connection:
            connection.execute(
                "INSERT INTO external_results (operation_id,destination,status,dispatched_at_ms,updated_at_ms)"
                " VALUES (?,?,'dispatched',?,?)", (operation_id, destination, at_ms, at_ms))

    def record(self, operation_id: str, status: str, *, object_id: str | None = None,
               version: Mapping[str, Any] | None = None, detail: str | None = None) -> None:
        with self._open() as connection:
            changed = connection.execute(
                "UPDATE external_results SET status=?,object_id=?,version_json=?,detail=?,updated_at_ms=?"
                " WHERE operation_id=?",
                (status, object_id, None if version is None else json.dumps(version, sort_keys=True),
                 detail, int(time.time() * 1000), operation_id)).rowcount
        if changed != 1:
            raise LedgerError("UNKNOWN_EXTERNAL_RESULT")

    def get(self, operation_id: str) -> dict[str, Any] | None:
        with self._open() as connection:
            row = connection.execute(
                "SELECT operation_id,destination,status,object_id,version_json,detail,dispatched_at_ms"
                " FROM external_results WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            return None
        keys = ("operation_id", "destination", "status", "object_id", "version", "detail", "dispatched_at_ms")
        record = dict(zip(keys, row))
        record["version"] = None if record["version"] is None else json.loads(record["version"])
        return record


# --- destination: GitHub Issues ------------------------------------------------

@dataclass(frozen=True)
class SendOutcome:
    kind: str  # confirmed | not_executed | rejected | unknown
    obj: Mapping[str, Any] | None = None
    wait_seconds: float = 0.0
    detail: str = ""


class LookupIncomplete(Exception):
    """The lookup could not cover the whole window; absence is not established."""


@dataclass
class GitHubIssueDestination:
    transport: Transport
    label: str = EVIDENCE_LABEL
    clock: Callable[[], float] = field(default=time.time)
    name: str = "github_issues"
    supports_idempotency: bool = False  # contracts/github_issue_destination.json

    @staticmethod
    def marker(operation_id: str) -> str:
        return f"vater-operation-id: {operation_id}"

    def payload(self, operation_id: str, title: str) -> dict[str, Any]:
        return {"title": title, "labels": [self.label],
                "body": "Created by the VATER external-change evidence run.\n\n" + self.marker(operation_id)}

    def send(self, operation_id: str, title: str) -> SendOutcome:
        try:
            reply = self.transport.request("POST", "/issues", json_body=self.payload(operation_id, title))
        except TransportError as error:
            return SendOutcome("unknown", detail=f"no reply: {error}")
        if reply.status == 201 and isinstance(reply.body, Mapping):
            return SendOutcome("confirmed", obj=reply.body)
        if 200 <= reply.status < 300:
            # Success without a usable body: the issue may exist. Reconcile, never resend.
            return SendOutcome("unknown", detail=f"HTTP {reply.status} without a usable body")
        wait = self._rate_limit_wait(reply)
        if wait is not None:
            return SendOutcome("not_executed", wait_seconds=wait, detail=f"rate limited: HTTP {reply.status}")
        if reply.status >= 500:
            return SendOutcome("unknown", detail=f"HTTP {reply.status}")
        return SendOutcome("rejected", detail=f"HTTP {reply.status}")

    def _rate_limit_wait(self, reply: HttpReply) -> float | None:
        """GitHub's rate-limit replies mean the request did not execute."""
        if reply.status not in (403, 429):
            return None
        headers = reply.headers
        if "retry-after" in headers:
            try:
                return max(0.0, float(headers["retry-after"]))
            except ValueError:
                return 60.0
        if headers.get("x-ratelimit-remaining") == "0":
            try:
                return max(0.0, float(headers.get("x-ratelimit-reset", "0")) - self.clock())
            except ValueError:
                return 60.0
        return 60.0 if reply.status == 429 else None

    def lookup(self, operation_id: str, dispatched_at_ms: int) -> list[Mapping[str, Any]]:
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(max(0, dispatched_at_ms - LOOKUP_MARGIN_MS) / 1000))
        marker, matches = self.marker(operation_id), []
        for page in range(1, MAX_LOOKUP_PAGES + 1):
            try:
                reply = self.transport.request("GET", "/issues", query={
                    "labels": self.label, "state": "all", "since": since, "per_page": 100, "page": page})
            except TransportError as error:
                raise LookupIncomplete(str(error)) from None
            if reply.status != 200 or not isinstance(reply.body, list):
                raise LookupIncomplete(f"HTTP {reply.status}")
            matches += [issue for issue in reply.body if isinstance(issue, Mapping)
                        and "pull_request" not in issue and marker in str(issue.get("body") or "").splitlines()]
            if len(reply.body) < 100:
                return matches
        raise LookupIncomplete("too many pages")

    def check(self, operation_id: str, title: str, obj: Mapping[str, Any]) -> bool:
        labels = {label.get("name") for label in obj.get("labels") or [] if isinstance(label, Mapping)}
        return ("pull_request" not in obj and obj.get("title") == title and self.label in labels
                and type(obj.get("number")) is int and obj["number"] > 0
                and self.marker(operation_id) in str(obj.get("body") or "").splitlines())

    def snapshot(self, number: int) -> tuple[Mapping[str, Any] | None, str | None]:
        """One GET of the issue: its body and the ETag of that same response."""
        try:
            reply = self.transport.request("GET", f"/issues/{number}")
        except TransportError:
            return None, None
        if reply.status != 200 or not isinstance(reply.body, Mapping):
            return None, None
        return reply.body, reply.headers.get("etag")

    @staticmethod
    def version(obj: Mapping[str, Any], etag: str | None) -> dict[str, Any]:
        """Version fields of ONE snapshot; etag must come from the response that returned obj."""
        labels = sorted(label.get("name") for label in obj.get("labels") or [] if isinstance(label, Mapping))
        canonical = json.dumps({"title": obj.get("title"), "body": obj.get("body"), "labels": labels},
                               sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return {"number": obj.get("number"), "node_id": obj.get("node_id"), "updated_at": obj.get("updated_at"),
                "etag": etag, "content_sha256": sha256(canonical.encode("utf-8")).hexdigest()}


# --- the tool ------------------------------------------------------------------

class IssueRequest(NamedTuple):
    request_key: str
    title: str


def validate_issue_request(arguments: Any) -> IssueRequest:
    if type(arguments) is not dict or set(arguments) != {"request_key", "title"}:
        raise OperationError("INVALID_ARGUMENT", "arguments require only request_key and title")
    key = validate_request_key(arguments["request_key"])
    title = arguments["title"]
    if (type(title) is not str or not title or title != title.strip()
            or len(title) > MAX_TITLE_CHARACTERS or not title.isprintable()):
        raise OperationError("INVALID_ARGUMENT", "title must be 1-120 printable characters")
    return IssueRequest(key, title)


def _verify_issue_number(_request: IssueRequest, value: Any) -> None:
    if type(value) is not int or value <= 0:
        raise OperationError("INVALID_TOOL_RESULT", "invalid issue number")


def _external_only(_request: IssueRequest) -> None:
    raise OperationError("UNSUPPORTED_OPERATION", "tool requires the durable operation path")


def github_test_create_issue_spec(contracts_dir: Path = DEFAULT_CONTRACTS_DIR) -> ToolSpec:
    def read(name: str) -> Any:
        return json.loads((contracts_dir / name).read_text(encoding="utf-8"))

    output = read("github_test_create_issue.output.contract.json")
    success = output["domains"]["success"]
    return ToolSpec(
        name=TOOL_NAME,
        description="Create one labelled test issue in the host-chosen GitHub repository; "
                    "requires a stable request_key and a host approval.",
        risk=read("tool_risks.json")["tools"][TOOL_NAME],
        input_schema=read("github_test_create_issue.input.schema.json"),
        output_schema={"type": success["type"], "properties": success["properties"],
                       "required": success["required"], "additionalProperties": False},
        validate=validate_issue_request, run=_external_only, verify_result=_verify_issue_number,
        contract_version=output["contract_version"],
    )


# Host hook: issue a ledger approval bound to this operation and these exact
# arguments, and return its id; return None to refuse. Never caller-controlled.
Approver = Callable[[Operation, Mapping[str, Any]], "str | None"]


class ExternalChangeExecutor:
    """Durable executor for ToolRuntime: ledger, approval, destination, result store."""

    def __init__(self, ledger: OperationLedger, store: ExternalResultStore,
                 destination: GitHubIssueDestination, *, principal: str, authority: LocalAuthority,
                 target_id: str, approve: Approver | None = None, max_attempts: int = 2,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be 1-5")
        self.ledger, self.store, self.destination = ledger, store, destination
        self.principal, self.authority, self.target_id = principal, authority, target_id
        self.approve, self.max_attempts, self.sleep = approve, max_attempts, sleep

    async def acall(self, name: str, arguments: Any) -> BridgeResult:
        import anyio
        # The work finishes and is recorded even if the caller is cancelled.
        return await anyio.to_thread.run_sync(self.call, name, arguments, abandon_on_cancel=False)

    def result(self, operation_id: str) -> dict[str, Any] | None:
        """EXTERNAL-012: the stored destination result for one of this
        principal's operations. Another principal's operation id reads as
        absent (as LEDGER-010 does for calculations), never as its result."""
        try:
            op = self.ledger.get(operation_id)
        except LedgerError:
            return None
        if op.principal != self.principal or op.tool_name != TOOL_NAME:
            return None
        return self.store.get(operation_id)

    @staticmethod
    def reserve_arguments(request: IssueRequest) -> dict[str, Any]:
        """What the ledger fingerprints and what an approval must be bound to."""
        return {"title": request.title}

    def call(self, name: str, arguments: Any) -> BridgeResult:
        # CONTRACT-FAILURE-001: only declared failure codes reach a client.
        return within_contract(self._call(name, arguments), declared_failures(TOOL_NAME))

    def _call(self, name: str, arguments: Any) -> BridgeResult:
        if name != TOOL_NAME:
            return _error("UNKNOWN_TOOL")
        try:
            request = validate_issue_request(arguments)
        except OperationError as exc:
            return _error(exc.code)
        if not self.authority.permits(TOOL_NAME):
            return _error("NOT_AUTHORIZED")
        try:
            op = self.ledger.reserve(principal=self.principal, request_key=request.request_key,
                                     tool_name=TOOL_NAME, target_id=self.target_id,
                                     resource_version="issues:create",
                                     arguments=self.reserve_arguments(request), requires_approval=True)
        except LedgerError as exc:
            return _error(exc.code)
        if not op.replayed or op.state == "prepared":
            return self._dispatch(op, request)
        return self._replay(op, request)

    def _replay(self, op: Operation, request: IssueRequest) -> BridgeResult:
        saved = self.store.get(op.operation_id)
        if saved is not None and saved["status"] in ("succeeded", "reconciled"):
            return BridgeResult(True, int(saved["object_id"]), op.operation_id, True, None)
        if op.state == "dispatched":
            return _error("IN_PROGRESS", op)  # another attempt may be in flight
        if op.state == "unknown" or (op.state == "succeeded" and (saved or {}).get("status") != "failed"):
            # A succeeded ledger entry without a stored result (written by an
            # older order, or lost) is recovered by lookup, never resent.
            return self._reconcile(op, request, replayed=True)
        return _error((saved or {}).get("detail") or "ALREADY_RECORDED", op, replayed=True)

    def _dispatch(self, op: Operation, request: IssueRequest) -> BridgeResult:
        approval_id = (self.approve(op, self.reserve_arguments(request))
                       if self.approve is not None else None)
        if approval_id is None:
            return _error("APPROVAL_REQUIRED", op)
        try:
            # EXTERNAL-011: durable before the request leaves this process.
            self.ledger.begin_dispatch(op.operation_id, approval_id=approval_id, authority=self.authority)
        except LedgerError as exc:
            return _error("IN_PROGRESS" if exc.code == "NOT_PREPARED" else exc.code, op)
        self.store.dispatched(op.operation_id, self.destination.name, int(time.time() * 1000))
        revoked = False
        for attempt in range(1, self.max_attempts + 1):
            outcome = self.destination.send(op.operation_id, request.title)
            if outcome.kind != "not_executed" or attempt == self.max_attempts:
                break
            # EXTERNAL-017: the destination proved the request did not execute.
            self.sleep(outcome.wait_seconds)
            # The grant may have expired or been revoked while waiting; a resend
            # is a new external effect and needs the live grant again.
            if not self.authority.permits(TOOL_NAME):
                revoked = True
                break
        if outcome.kind == "confirmed":
            return self._confirm(op, request, outcome.obj, replayed=False)
        if outcome.kind in ("not_executed", "rejected"):
            code = ("NOT_AUTHORIZED" if revoked else "RATE_LIMITED" if outcome.kind == "not_executed"
                    else "REJECTED_BY_DESTINATION")
            # Store first: a crash before the ledger write leaves the ledger
            # dispatched (unknown after restart), never a ledger claim without a record.
            self.store.record(op.operation_id, "failed", detail=code)
            self.ledger.record_definitive_result(op.operation_id, "failed")
            return _error(code, op)
        # EXTERNAL-015: no destination idempotency, so never send again.
        return self._reconcile(op, request, replayed=False)

    def _confirm(self, op: Operation, request: IssueRequest, obj: Mapping[str, Any],
                 *, replayed: bool, reconciled: bool = False) -> BridgeResult:
        state = self.ledger.get(op.operation_id).state
        # EXTERNAL-021: the version is taken from one fresh snapshot, which must
        # itself still match the request; without a snapshot, no ETag is claimed.
        current, etag = (self.destination.snapshot(obj["number"])
                         if type(obj.get("number")) is int else (None, None))
        verified = current if current is not None else obj
        if not (self.destination.check(op.operation_id, request.title, obj)
                and self.destination.check(op.operation_id, request.title, verified)):
            if state == "dispatched":
                self.ledger.mark_unknown(op.operation_id)
            self.store.record(op.operation_id, "human_required", detail="TARGET_MISMATCH")
            return _error("TARGET_MISMATCH", op, replayed=replayed)
        version = self.destination.version(verified, etag if current is not None else None)
        # The result is stored BEFORE the ledger claims success, so a crash in
        # between can never leave a succeeded operation without its result. A
        # ledger operation already unknown or succeeded is not rewritten.
        self.store.record(op.operation_id, "reconciled" if reconciled else "succeeded",
                          object_id=str(obj["number"]), version=version)
        if state == "dispatched":
            self.ledger.record_definitive_result(op.operation_id, "succeeded")
        return BridgeResult(True, obj["number"], op.operation_id, replayed, None)

    def _reconcile(self, op: Operation, request: IssueRequest, *, replayed: bool) -> BridgeResult:
        saved = self.store.get(op.operation_id)
        if saved is None:
            # A crash between begin_dispatch and the store write: no request time
            # was recorded, so the lookup window starts now (minus the margin).
            self.store.dispatched(op.operation_id, self.destination.name, int(time.time() * 1000))
            saved = self.store.get(op.operation_id)
        dispatched_at = saved["dispatched_at_ms"]
        try:
            matches = self.destination.lookup(op.operation_id, dispatched_at)
            detail = {0: "NO_MATCH", 1: None}.get(len(matches), "MANY_MATCHES")
        except LookupIncomplete:
            matches, detail = [], "LOOKUP_INCOMPLETE"
        if detail is None:
            return self._confirm(op, request, matches[0], replayed=replayed, reconciled=True)
        # EXTERNAL-016: absence is not proven; a person reconciles.
        if self.ledger.get(op.operation_id).state == "dispatched":
            self.ledger.mark_unknown(op.operation_id)
        self.store.record(op.operation_id, "human_required", detail=detail)
        return _error("HUMAN_RECONCILIATION_REQUIRED", op, replayed=replayed)


def _error(code: str, op: Operation | None = None, *, replayed: bool = False) -> BridgeResult:
    return BridgeResult(False, None, op.operation_id if op is not None else None, replayed, code)
