"""Cross-adapter conformance: the same vectors through every protocol adapter.

A common foundation is real only if every adapter gives the same answer as
the shared runtime. For each independent vector in contracts/:

  runtime  must equal the vector's expected outcome
  MCP      must equal the runtime exactly (value and error code)
  HTTP     must equal the runtime for every call it executes. A call the
           runtime rejects before execution, HTTP rejects at acceptance:
           404 UNKNOWN_TOOL for an unserved name, 400 INVALID_BODY for input
           errors, 403 NOT_AUTHORIZED for grants.
           This mapping is the HTTP adapter's documented contract
           (docs/LOCAL_HTTP.md): a 400 body carries only {"error"}.

Adding a tool means adding its vectors here; no adapter code changes.
"""
import json
from pathlib import Path

import pytest

from adapter_drivers import Outcome, TOKEN, http_outcome, load_mcp_adapter, mcp_outcome, runtime_outcome
from verified_execution.arithmetic import checked_multiply_spec
from verified_execution.core import LocalAuthority
from verified_execution.runtime import ToolRuntime
from verified_execution.tools import default_registry

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
VECTOR_FILES = ("expected_cases.jsonl", "checked_multiply.cases.jsonl")
VECTORS = [json.loads(line) for name in VECTOR_FILES
           for line in (CONTRACTS / name).read_text().splitlines() if line.strip()]
REGISTRY = default_registry().with_tools(checked_multiply_spec())
AUTHORITY = LocalAuthority(frozenset({"checked_add", "checked_multiply"}))

# Pre-execution rejections: HTTP refuses these before creating a job.
ACCEPTANCE_REJECTION = {
    ("received", "rejected"): (400, "INVALID_BODY"),
    ("received", "validated", "rejected"): (403, "NOT_AUTHORIZED"),
}


def expected(vector):
    e = vector["expected"]
    return Outcome(e["ok"], e["value"], e["error_code"])


@pytest.fixture(scope="module")
def mcp_server():
    pytest.importorskip("mcp")
    return load_mcp_adapter("server_mcp_conformance").build_server(AUTHORITY, registry=REGISTRY)


@pytest.fixture(scope="module")
def http_app():
    """One HTTP app serving every read-only tool in the registry."""
    pytest.importorskip("starlette")
    from verified_execution.http_local import create_app
    return create_app(TOKEN, AUTHORITY, registry=REGISTRY)


def test_vectors_cover_every_registered_tool():
    assert {vector["name"] for vector in VECTORS} >= REGISTRY.names()
    assert len({vector["case"] for vector in VECTORS}) == len(VECTORS)


@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: vector["case"])
def test_runtime_matches_vector(vector):
    outcome, _ = runtime_outcome(ToolRuntime(AUTHORITY, REGISTRY), vector["name"], vector["arguments"])
    assert outcome == expected(vector)


@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: vector["case"])
def test_mcp_matches_runtime(vector, mcp_server):
    assert mcp_outcome(mcp_server, vector["name"], vector["arguments"]) == expected(vector)


@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: vector["case"])
def test_http_matches_runtime(vector, http_app):
    reference, states = runtime_outcome(ToolRuntime(AUTHORITY, REGISTRY), vector["name"], vector["arguments"])
    actual = http_outcome(http_app, vector["arguments"], tool=vector["name"])
    if reference.error_code == "UNKNOWN_TOOL":
        # /tools/{name}/jobs for a name the host does not serve.
        assert actual == Outcome(False, None, "UNKNOWN_TOOL", stage="acceptance")
    elif states in ACCEPTANCE_REJECTION:
        _status, code = ACCEPTANCE_REJECTION[states]
        assert actual == Outcome(False, None, code, stage="acceptance")
    else:
        assert actual == reference


@pytest.mark.parametrize("vector", [v for v in VECTORS if v["name"] == "checked_add"],
                         ids=lambda vector: vector["case"])
def test_http_version_one_route_matches_named_route(vector, http_app):
    """POST /jobs keeps serving checked_add exactly like /tools/checked_add/jobs."""
    assert http_outcome(http_app, vector["arguments"]) == \
        http_outcome(http_app, vector["arguments"], tool="checked_add")


def test_http_grant_refusal_matches_runtime():
    """The authorization row of the mapping, exercised through HTTP, per tool."""
    pytest.importorskip("starlette")
    from verified_execution.http_local import create_app
    denied = LocalAuthority(frozenset({"checked_add"}))
    _, states = runtime_outcome(ToolRuntime(denied, REGISTRY), "checked_multiply", {"left": 2, "right": 3})
    app = create_app(TOKEN, denied, registry=REGISTRY)
    assert ACCEPTANCE_REJECTION[states] == (403, "NOT_AUTHORIZED")
    assert http_outcome(app, {"left": 2, "right": 3}, tool="checked_multiply") == \
        Outcome(False, None, "NOT_AUTHORIZED", stage="acceptance")
    # The same app still runs the tool the host did grant.
    assert http_outcome(app, {"left": 2, "right": 3}, tool="checked_add") == Outcome(True, 5, None)
