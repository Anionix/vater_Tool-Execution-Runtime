import http.client
import json
import socket
from threading import Event, Thread
from time import monotonic, sleep

import pytest

pytest.importorskip("uvicorn")
pytest.importorskip("starlette")

import uvicorn

from verified_execution.core import CalculationService, LocalAuthority
from verified_execution.http_local import LOOPBACK, MAX_BYTES, MAX_DEPTH, create_app

TOKEN = "testOnlyCredential" * 3


@pytest.fixture
def serve():
    servers = []

    def start(authority=None):
        sock = socket.socket()
        sock.bind((LOOPBACK, 0))
        host, port = sock.getsockname()
        server = uvicorn.Server(uvicorn.Config(create_app(TOKEN, authority), log_level="error"))
        thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        servers.append((server, thread, sock))
        thread.start()
        deadline = monotonic() + 3
        while not server.started and monotonic() < deadline:
            sleep(0.01)
        assert server.started and host == "127.0.0.1"

        def request(method, path, body=None, *, token=TOKEN, media="application/json", chunked=False):
            headers = {"Content-Type": media}
            if token is not None:
                headers["Authorization"] = f"Bearer {token}"
            connection = http.client.HTTPConnection(host, port, timeout=3)
            try:
                connection.request(method, path, body=body, headers=headers, encode_chunked=chunked)
                result = connection.getresponse()
                assert result.getheader("Cache-Control") == "no-store"
                return result.status, json.loads(result.read())
            finally:
                connection.close()
        return request
    yield start
    for server, thread, sock in servers:
        server.should_exit = True
        thread.join(5)
        sock.close()
        assert not thread.is_alive()


def test_real_http_routes_match_contract(serve, monkeypatch):
    entered, finish = Event(), Event()
    execute = CalculationService.execute

    def hold(service, name, arguments):
        entered.set()
        assert finish.wait(3)
        return execute(service, name, arguments)

    monkeypatch.setattr(CalculationService, "execute", hold)
    request = serve()
    _, specification = request("GET", "/openapi.json")
    paths = specification["paths"]
    assert set(paths) == {"/jobs", "/jobs/{job_id}"}
    assert set(paths["/jobs"]) == {"post"}
    assert set(paths["/jobs/{job_id}"]) == {"get", "delete"}

    def verify(method, path, status, body=None, **kwargs):
        result, content = request(method, path, body, **kwargs)
        contract_path = path if path == "/jobs" else "/jobs/{job_id}"
        assert result == status
        assert str(status) in paths[contract_path][method.lower()]["responses"]
        return content

    try:
        payload = b'{"left":1,"right":2}'
        running = "/jobs/" + verify("POST", "/jobs", 202, payload)["job_id"]
        assert entered.wait(2)
        queued = "/jobs/" + verify("POST", "/jobs", 202, payload)["job_id"]
        assert verify("GET", queued, 200)["state"] == "prepared"
        assert verify("DELETE", queued, 200)["state"] == "cancelled"
        verify("DELETE", running, 409)
        verify("GET", "/jobs/missing", 404)
        verify("DELETE", "/jobs/missing", 404)
        verify("POST", "/jobs", 401, payload, token="secret-not-the-token")
    finally:
        finish.set()
    deadline = monotonic() + 2
    while monotonic() < deadline:
        result = verify("GET", running, 200)
        if result["state"] == "succeeded":
            break
        sleep(0.01)
    assert result == {"state": "succeeded", "result": 3, "error": None}
    assert verify("GET", queued, 200)["state"] == "cancelled"


def test_real_http_rejects_body_before_job_creation(serve):
    request = serve()
    for payload, expected in (
        (b'{"left":1,"right":2,"principal":"fake"}', 400),
        (b'{"left":true,"right":2}', 400),
        (b'{"left":1,"left":2,"right":3}', 400),
        (b"[" * (MAX_DEPTH + 1) + b"]" * (MAX_DEPTH + 1), 400),
        (iter([b"x" * MAX_BYTES, b"x"]), 413),
        (b'{"token":"secret-body-value"}', 400),
    ):
        status, body = request("POST", "/jobs", payload, chunked=expected == 413)
        assert status == expected
        assert set(body) == {"error"}
        assert "secret-body-value" not in str(body)
    assert request("POST", "/jobs", b"{}", media="text/plain")[0] == 415


def test_real_http_requires_host_credential_and_common_grant(serve):
    denied = serve(LocalAuthority(frozenset()))
    assert denied("GET", "/openapi.json", token=None)[0] == 401
    assert denied("POST", "/jobs", b'{"left":1,"right":2}') == (403, {"error": "NOT_AUTHORIZED"})
    with pytest.raises(ValueError, match="host-configured"):
        create_app("")
