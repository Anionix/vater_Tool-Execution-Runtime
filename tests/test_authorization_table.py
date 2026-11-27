"""task.testing.003: the authority is checked against an independent table.

tests/fixtures/authorization_table.json holds literal expected outcomes, one
row per case, written from the documented policy (see its _comment). These
tests read each expected outcome from a row and compare it with what the
implementation does. Nothing here re-computes an expectation, and
test_expectations_do_not_copy_the_implementation checks mechanically that
neither this module nor the table contains the implementation's condition.
"""
from __future__ import annotations

import asyncio
import inspect
import itertools
import json
from pathlib import Path
import re
from threading import Event
from time import monotonic_ns

import pytest

from adapter_drivers import TOKEN, asgi, load_mcp_adapter, with_sdk_client
from verified_execution import core
from verified_execution.arithmetic import checked_multiply_spec
from verified_execution.core import LOCAL_PUBLISHER, LocalAuthority
from verified_execution.durable import checked_add_recorded_spec
from verified_execution.http_local import create_app
from verified_execution.ledger import OperationLedger
from verified_execution.recorded_tool_bridge import RecordedToolBridge
from verified_execution.runtime import ToolRuntime
from verified_execution.tools import default_registry

TABLE_PATH = Path(__file__).parent / "fixtures" / "authorization_table.json"
TABLE = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
REGISTRY = default_registry().with_tools(checked_multiply_spec(), checked_add_recorded_spec())
HOUR_NS = 3_600_000_000_000


def grant_id(row):
    return "-".join(str(row[key]) for key in ("tool", "publisher", "destination", "revocation", "expiry"))


def expiry(state):
    return {"absent": None, "future": monotonic_ns() + HOUR_NS, "past": monotonic_ns() - 1}[state]


def revocation(state):
    if state == "absent":
        return None
    event = Event()
    if state == "set":
        event.set()
    return event


# --- the grant itself --------------------------------------------------------------

@pytest.mark.parametrize("row", TABLE["grant_check"], ids=grant_id)
def test_grant_check_matches_the_table(row):
    fixture = TABLE["grant_fixture"]
    authority = LocalAuthority(frozenset(fixture["allowed_tools"]),
                               allowed_destinations=frozenset(fixture["allowed_destinations"]),
                               expires_at_monotonic_ns=expiry(row["expiry"]),
                               revocation_event=revocation(row["revocation"]))
    assert authority.permits(row["tool"], publisher=row["publisher"], destination=row["destination"]) \
        is row["allowed"]


def test_table_rows_are_complete_and_self_consistent():
    rows = TABLE["grant_check"]
    keys = ("tool", "publisher", "destination", "revocation", "expiry")
    values = [sorted({row[key] for row in rows}, key=str) for key in keys]
    combinations = {tuple(row[key] for key in keys) for row in rows}
    # Every combination of the listed values appears exactly once.
    assert len(combinations) == len(rows) == len(list(itertools.product(*values)))
    assert LOCAL_PUBLISHER in values[1] and None in values[2]
    for row in rows:
        assert type(row["allowed"]) is bool and row["allowed"] is (row["deny_reasons"] == [])
    pairs = {(row["grant"], row["call"]) for row in TABLE["end_to_end"]}
    assert pairs == set(itertools.product(TABLE["grants"], TABLE["calls"]))


def test_expectations_do_not_copy_the_implementation():
    """The condition inside LocalAuthority.permits must not appear here or in the table."""
    body = inspect.getsource(core.LocalAuthority.permits).split('"""')[-1]
    clauses = {re.sub(r"\s+", " ", clause).strip(" ()") for clause in re.split(r"\band\b|\breturn\b", body)}
    clauses = {clause for clause in clauses if len(clause) > 12}
    assert clauses, "could not read the implementation's condition"
    ours = re.sub(r"\s+", " ", Path(__file__).read_text(encoding="utf-8") + TABLE_PATH.read_text(encoding="utf-8"))
    assert [clause for clause in clauses if clause in ours] == []


# --- end to end through every adapter ---------------------------------------------------

def authority_for(grant):
    spec = TABLE["grants"][grant]
    return LocalAuthority(frozenset(spec["tools"]),
                          expires_at_monotonic_ns=monotonic_ns() - 1 if spec["state"] == "expired" else None,
                          revocation_event=revocation("set") if spec["state"] == "revoked" else None)


def end_to_end_id(row):
    return f"{row['grant']}-{row['call']}"


def matches(expected, ok, value, error_code):
    if isinstance(expected, dict):
        return ok and value == expected["ok"]
    return not ok and error_code == expected


@pytest.mark.parametrize("row", TABLE["end_to_end"], ids=end_to_end_id)
def test_runtime_matches_the_table(row, tmp_path):
    call = TABLE["calls"][row["call"]]
    authority = authority_for(row["grant"])
    durable = ({"checked_add_recorded": RecordedToolBridge(OperationLedger(tmp_path / "l.sqlite3"),
                                                           principal="host", authority=authority)}
               if call["durable_executor"] else None)
    outcome = asyncio.run(ToolRuntime(authority, REGISTRY, durable=durable).acall(call["tool"], call["arguments"]))
    assert matches(row["runtime"], outcome.ok, outcome.value, outcome.error_code), outcome


@pytest.mark.parametrize("row", TABLE["end_to_end"], ids=end_to_end_id)
def test_mcp_matches_the_table(row, tmp_path):
    pytest.importorskip("mcp")
    call = TABLE["calls"][row["call"]]
    server = load_mcp_adapter("server_mcp_authorization_table").build_server(
        authority_for(row["grant"]), registry=REGISTRY,
        ledger_path=str(tmp_path / "l.sqlite3") if call["durable_executor"] else None)

    async def scenario(client):
        return await client.call_tool(call["tool"], call["arguments"])

    reply = with_sdk_client(server, scenario)
    value = None if reply.is_error else reply.structured_content["result"]
    assert matches(row["mcp"], not reply.is_error, value, reply.content[0].text if reply.is_error else None), reply


@pytest.mark.parametrize("row", TABLE["end_to_end"], ids=end_to_end_id)
def test_http_matches_the_table(row):
    call = TABLE["calls"][row["call"]]
    app = create_app(TOKEN, authority_for(row["grant"]), registry=REGISTRY)

    async def scenario():
        status, reply = await asgi(app, "POST", f"/tools/{call['tool']}/jobs", json.dumps(call["arguments"]).encode())
        if status != 202:
            return {"status": status, "error": reply["error"]}
        for _ in range(500):
            _, job = await asgi(app, "GET", f"/jobs/{reply['job_id']}")
            if job["state"] not in {"prepared", "running"}:
                return {"status": status, "job": {"state": job["state"], "result": job["result"]}}
            await asyncio.sleep(0.01)
        raise AssertionError("job did not finish")

    assert asyncio.run(scenario()) == row["http"]
