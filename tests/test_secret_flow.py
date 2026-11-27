"""task.testing.017: secret values in external data never reach a destination
that is not allowed.

Secrets here are the host's destination credential, text that a destination
sends back, and text supplied to a tool or a model request. Each test plants
a distinct marker value and then checks every place the value could leak:
another network origin, the SQLite files, stored results, returned values and
error replies.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import pytest

from adapter_drivers import TOKEN, asgi, load_mcp_adapter, with_sdk_client
from test_external_change import GRANT, REPO, FakeGitHub, Host
from verified_execution.external_change import (
    TOOL_NAME, ExternalChangeExecutor, ExternalResultStore, GitHubIssueDestination, HttpReply, UrllibTransport)
from verified_execution.http_local import create_app
from verified_execution.ledger import OperationLedger
from verified_execution.model_mock import ModelContractError, ModelMessage, ModelRequest, OfflineResponses

CREDENTIAL = "ghp_vaterTestCredential0123456789abcdef"
SECRET = "sk_live_vaterPlantedSecret_9f3a7c1e"


class Recorder(BaseHTTPRequestHandler):
    """Records every request; replies with whatever the server's `reply` says."""

    def _handle(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.seen.append((self.command, self.path, dict(self.headers), body))
        status, headers, payload = self.server.reply(self.command, self.path)
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        data = json.dumps(payload).encode() if payload is not None else b""
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PATCH = _handle

    def log_message(self, *args):
        pass


@contextmanager
def http_server(reply):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    server.seen, server.reply = [], reply
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def stored_bytes(path: Path) -> bytes:
    """Every byte SQLite left on disk for this database, journals included."""
    return b"".join(candidate.read_bytes() for candidate in path.parent.glob(path.name + "*"))


# --- the destination credential stays with the configured API host ------------

@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_credential_never_follows_a_redirect(method, code):
    with http_server(lambda *_: (200, {}, {"stolen": True})) as other:
        target = f"http://127.0.0.1:{other.server_port}/collect"
        with http_server(lambda *_: (code, {"Location": target}, None)) as api:
            transport = UrllibTransport(REPO, CREDENTIAL, base_url=f"http://127.0.0.1:{api.server_port}")
            reply = transport.request(method, "/issues", json_body={"title": "t"} if method == "POST" else None)
    assert reply.status == code  # the redirect is returned, not followed
    assert other.seen == []      # the other origin received no request at all
    assert [headers["Authorization"] for _, _, headers, _ in api.seen] == [f"Bearer {CREDENTIAL}"]


def test_redirected_create_is_a_definitive_rejection(tmp_path):
    with http_server(lambda *_: (200, {}, {"number": 1})) as other:
        target = f"http://127.0.0.1:{other.server_port}/repos/{REPO}/issues"
        with http_server(lambda *_: (307, {"Location": target}, None)) as api:
            path = tmp_path / "ledger.sqlite3"
            transport = UrllibTransport(REPO, CREDENTIAL, base_url=f"http://127.0.0.1:{api.server_port}")
            host = Host(path, None)
            host.executor = ExternalChangeExecutor(
                host.ledger, ExternalResultStore(path), GitHubIssueDestination(transport), principal="local-host",
                authority=GRANT, target_id=REPO, approve=host.approve)
            result = host.create(key="redirected")
    assert result.error_code == "REJECTED_BY_DESTINATION"
    assert host.state(result.operation_id) == "failed"
    assert [method for method, *_ in api.seen] == ["POST"] and other.seen == []


# --- text a destination sends back is kept only as a digest ----------------------

class LeakyGitHub(FakeGitHub):
    """A destination whose replies carry a secret in the body, a field and the headers."""

    def _create(self, body):
        issue = super()._create({**body, "body": body["body"] + f"\n\ntoken={SECRET}"})
        issue["assignee_note"] = SECRET
        return issue

    def request(self, method, path, *, json_body=None, query=None):
        reply = super().request(method, path, json_body=json_body, query=query)
        return HttpReply(reply.status, {**reply.headers, "x-debug-token": SECRET}, reply.body)


def test_destination_replies_are_stored_as_digests_only(tmp_path):
    github = LeakyGitHub()
    host = Host(tmp_path / "ledger.sqlite3", github)
    result = host.create()
    assert result.ok and result.value == 1 and SECRET in github.issues[0]["body"]
    assert SECRET not in json.dumps(host.store.get(result.operation_id))
    assert SECRET.encode() not in stored_bytes(tmp_path / "ledger.sqlite3")
    assert SECRET not in repr(result)  # the caller gets the issue number only


def test_rejection_text_from_the_destination_is_not_copied(tmp_path):
    class Rejecting(FakeGitHub):
        def request(self, method, path, *, json_body=None, query=None):
            if method == "POST":
                self.posts += 1
                return HttpReply(422, {"x-debug-token": SECRET}, {"message": f"bad token {SECRET}"})
            return super().request(method, path, json_body=json_body, query=query)

    host = Host(tmp_path / "ledger.sqlite3", Rejecting())
    result = host.create()
    assert result.error_code == "REJECTED_BY_DESTINATION"
    assert host.store.get(result.operation_id)["detail"] == "REJECTED_BY_DESTINATION"
    assert SECRET.encode() not in stored_bytes(tmp_path / "ledger.sqlite3")


def test_tool_arguments_go_to_the_approved_destination_only(tmp_path):
    github = FakeGitHub()
    host = Host(tmp_path / "ledger.sqlite3", github)
    result = host.create(title=f"Rotate {SECRET}")
    assert result.ok and github.issues[0]["title"] == f"Rotate {SECRET}"  # the approved destination
    # LEDGER-008: the ledger and the result store keep fingerprints, not argument text.
    assert SECRET.encode() not in stored_bytes(tmp_path / "ledger.sqlite3")


# --- model requests ------------------------------------------------------------------

@pytest.mark.parametrize("field,value", [("destination", "remote-provider"), ("processing_region", "other-region")])
def test_model_input_never_reaches_a_destination_that_is_not_allowed(field, value):
    calls = []
    adapter = OfflineResponses(lambda payload: calls.append(payload) or {"output": []})
    request = ModelRequest((ModelMessage("user", f"my key is {SECRET}"),), **{field: value})
    with pytest.raises(ModelContractError) as raised:
        adapter.respond(request)
    assert calls == [] and SECRET not in str(raised.value)


# --- error replies do not echo what the caller sent ---------------------------------

@pytest.mark.parametrize("body", [
    {"left": 1, "right": 2, "note": SECRET},
    {"left": SECRET, "right": 2},
    {SECRET: 1, "left": 1, "right": 2},
])
def test_http_rejections_do_not_echo_request_text(body):
    status, reply = asyncio.run(asgi(create_app(TOKEN), "POST", "/jobs", json.dumps(body).encode()))
    assert status == 400 and SECRET not in json.dumps(reply)


def test_http_rejection_of_invalid_json_does_not_echo_it():
    body = f'{{"left": 1, "right": "{SECRET}"'.encode()
    status, reply = asyncio.run(asgi(create_app(TOKEN), "POST", "/jobs", body))
    assert status == 400 and SECRET not in json.dumps(reply)


@pytest.mark.parametrize("arguments", [
    {"left": 1, "right": 2, "note": SECRET},
    {"left": SECRET, "right": 2},
])
def test_mcp_rejections_do_not_echo_arguments(arguments):
    server = load_mcp_adapter("server_mcp_secret_flow").build_server()

    async def scenario(client):
        return await client.call_tool("checked_add", arguments)

    reply = with_sdk_client(server, scenario)
    assert reply.is_error
    assert SECRET not in json.dumps([block.model_dump(mode="json") for block in reply.content])
    assert reply.structured_content is None or SECRET not in json.dumps(reply.structured_content)
