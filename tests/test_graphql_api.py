"""task.optional_more_protocols.001-003: the GraphQL read prototype (src/vater_optional/graphql_api.py).

Runs only with requirements-optional-py313.lock (graphql-core 3.3.2); the base jobs skip it.
001 measures, over real loopback HTTP, what the local REST interface needs for questions a
single GraphQL request answers. 002 sends deep, wide, aliased, fragment-amplified and oversized
queries and checks that none reaches a resolver. 003 has two principals with their own jobs
and checks every way a query could reach the other principal's data.
"""
from __future__ import annotations

import http.client
from importlib import util
import json
from pathlib import Path

import pytest

pytest.importorskip("graphql")
pytest.importorskip("uvicorn")

from vater_optional import graphql_api  # noqa: E402
from vater_optional.graphql_api import Gateway, JobRecord, create_app  # noqa: E402
from verified_execution.tools import default_registry  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "optional" / "MORE_PROTOCOLS.md").read_text(encoding="utf-8")
ALICE, BOB = "alice-credential-0123456789abcdef", "bob-credential-0123456789abcdef-x"


def jobs() -> list[JobRecord]:
    return ([JobRecord(f"alice-{n}", "alice", "checked_add", "succeeded", n) for n in range(30)]
            + [JobRecord("alice-failed", "alice", "checked_add", "failed", None, "OVERFLOW")]
            + [JobRecord(f"bob-{n}", "bob", "checked_add", "succeeded", 1000 + n) for n in range(5)])


@pytest.fixture
def gateway():
    return Gateway(jobs(), default_registry())


def demand_script():
    spec = util.spec_from_file_location("measure_graphql_demand", ROOT / "scripts" / "measure_graphql_demand.py")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_queries_rest_cannot_answer_in_one_request_are_measured():
    """001: REST needs a request per job, and cannot list jobs it was not told about; GraphQL needs one."""
    record = json.loads((ROOT / "evidence" / "optional" / "graphql-demand.json").read_text(encoding="utf-8"))
    count = record["jobs"]
    assert count == 20
    assert record["rest"]["status_of_known_jobs"]["requests"] == count
    assert record["rest"]["jobs_with_tool_schema"]["requests"] == count + 1
    assert record["rest"]["succeeded_jobs_by_state"]["requests"] is None  # no list route at all
    assert {entry["requests"] for entry in record["graphql"].values()} == {1}
    assert (record["graphql"]["jobs_with_tool_schema"]["response_bytes"]
            < record["rest"]["jobs_with_tool_schema"]["response_bytes"])
    section = DOC.split("## GraphQL への利用要求", 1)[1].split("\n## ", 1)[0]
    for side in ("rest", "graphql"):
        for entry in record[side].values():
            if entry["response_bytes"] is not None:
                assert f"{entry['response_bytes']:,}" in section, entry
    assert "リリースには導入しない" in section
    fresh = demand_script().measure(jobs=4)  # the measurement itself still runs against the real REST app
    assert fresh["rest"]["status_of_known_jobs"]["requests"] == 4
    assert fresh["graphql"]["succeeded_jobs_by_state"]["requests"] == 1


DEEP = "{ jobs { tool { name } } __schema { types { fields { type { ofType { ofType { name } } } } } } }"
WIDE = "{ jobs(first: 50) { id tool { name description inputSchema } } " \
       "more: jobs(first: 50) { id tool { name description inputSchema } } }"
ALIASES = "{ " + " ".join(f"a{n}: tools {{ name }}" for n in range(16)) + " }"
FRAGMENTS = ("fragment F on Job { id tool { name description inputSchema } } "
             "{ a: jobs(first: 40) { ...F } b: jobs(first: 40) { ...F } }")


@pytest.mark.parametrize("query, variables, code", [
    (DEEP, None, "QUERY_TOO_DEEP"),
    ("{ jobs(first: 51) { id } }", None, "LIST_TOO_LARGE"),
    ("query Q($n: Int) { jobs(first: $n) { id } }", {"n": 500}, "LIST_TOO_LARGE"),
    (WIDE, None, "QUERY_TOO_COSTLY"),
    (FRAGMENTS, None, "QUERY_TOO_COSTLY"),
    (ALIASES, None, "TOO_MANY_ALIASES"),
    ("{ " + "tools { name } " * 200 + "}", None, "QUERY_TOO_LARGE"),
    ("{ jobs { id " + " " * 5000 + "} }", None, "QUERY_TOO_LARGE"),
    ("{ jobs { id nonexistent } }", None, "GRAPHQL_VALIDATION_FAILED"),
    ("fragment A on Job { ...B } fragment B on Job { ...A } { jobs { ...A } }", None, "GRAPHQL_VALIDATION_FAILED"),
    ("{ jobs { id ", None, "GRAPHQL_PARSE_FAILED"),
], ids=["deep", "first_literal", "first_variable", "wide", "fragments", "aliases", "tokens", "bytes",
        "unknown_field", "fragment_cycle", "parse"])
def test_an_excessive_query_is_refused_before_any_resolver_runs(gateway, query, variables, code):
    """002: depth, list size, cost, aliases, tokens and bytes are limited; a refusal runs nothing."""
    answer = gateway.execute("alice", query, variables)
    assert "data" not in answer and answer["errors"][0]["extensions"]["code"] == code, answer
    assert gateway.resolver_calls == 0


def test_the_limits_count_through_fragments_and_allow_ordinary_queries(gateway):
    from graphql import parse
    found = graphql_api.measure(parse(FRAGMENTS))
    assert found["cost"] > graphql_api.MAX_COST and found["depth"] == 3 and found["aliases"] == 2
    ordinary = gateway.execute("alice", "{ jobs(first: 5) { id state result tool { name } } tools { name } }")
    assert "errors" not in ordinary and len(ordinary["data"]["jobs"]) == 5
    introspection = gateway.execute("alice", "{ __schema { queryType { name } } }")
    assert introspection["data"]["__schema"]["queryType"]["name"] == "Query"


@pytest.mark.parametrize("query, variables", [
    ('{ job(id: "bob-0") { id result } }', None),
    ("query Q($id: ID!) { job(id: $id) { id result } }", {"id": "bob-1"}),
    ('{ mine: job(id: "alice-1") { id } theirs: job(id: "bob-2") { id result } }', None),
    ('fragment J on Job { id result } { job(id: "bob-3") { ...J } }', None),
    ('{ job(id: "bob-4") { ... on Job { id result } } }', None),
], ids=["by_id", "variable", "alias", "fragment", "inline_fragment"])
def test_another_principals_job_cannot_be_read_in_any_form(gateway, query, variables):
    """003: another principal's job answers exactly like a job that does not exist."""
    answer = gateway.execute("alice", query, variables)
    assert "errors" not in answer
    text = json.dumps(answer)
    assert "bob-" not in text and "100" not in text  # neither bob's ids nor his results (1000-1004)
    missing = gateway.execute("alice", '{ job(id: "no-such-job") { id } }')
    assert missing == {"data": {"job": None}}


def test_lists_hold_only_the_principals_own_jobs_and_no_owner_is_exposed(gateway):
    mine = gateway.execute("alice", "{ jobs(first: 50) { id } failed: jobs(state: failed) { id error } }")
    ids = [job["id"] for job in mine["data"]["jobs"]]
    assert len(ids) == 31 and all(job.startswith("alice-") for job in ids)
    assert mine["data"]["failed"] == [{"id": "alice-failed", "error": "OVERFLOW"}]
    theirs = gateway.execute("bob", "{ jobs(first: 50) { id result } }")
    assert [job["id"] for job in theirs["data"]["jobs"]] == [f"bob-{n}" for n in reversed(range(5))]
    names = set()
    for name, kind in graphql_api.SCHEMA.type_map.items():
        if not name.startswith("__") and hasattr(kind, "fields"):
            for field_name, field in kind.fields.items():
                names |= {field_name.lower(), *(argument.lower() for argument in field.args)}
    assert not names & {"owner", "principal", "user", "as", "impersonate"}, names  # nothing names another principal
    with pytest.raises(ValueError):
        gateway.execute("", "{ tools { name } }")


def test_the_http_principal_comes_only_from_the_credential(gateway):
    serve = demand_script().serve
    with serve(create_app(gateway, {ALICE: "alice", BOB: "bob"})) as port:
        def post(token, body: bytes):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
                connection.request("POST", "/graphql", body=body, headers=headers)
                reply = connection.getresponse()
                return reply.status, reply.getheader("Cache-Control"), json.loads(reply.read())
            finally:
                connection.close()

        query = json.dumps({"query": '{ job(id: "bob-0") { id result } }', "variables": {"principal": "bob"}}).encode()
        assert post(ALICE, query)[2] == {"data": {"job": None}}  # a principal claimed in the body is ignored
        assert post(BOB, query)[2] == {"data": {"job": {"id": "bob-0", "result": 1000}}}
        for token in (None, "wrong-credential-0123456789abcdef", ALICE[:-1]):
            status, cache, _ = post(token, query)
            assert status == 401 and cache == "no-store"
        assert post(ALICE, json.dumps({"query": DEEP}).encode())[0] == 400
        assert post(ALICE, b'{"nothing": 1}')[0] == 400
        assert post(ALICE, b"{" + b" " * 5000 + b"}")[0] == 400
    with pytest.raises(ValueError):
        create_app(gateway, {"short": "alice"})
