#!/usr/bin/env python3
"""task.optional_more_protocols.001: what the REST interface cannot answer in one request, measured.

  python scripts/measure_graphql_demand.py --output evidence/optional/graphql-demand.json

Serves the real local HTTP interface (verified_execution.http_local) and the GraphQL
prototype on loopback, creates N checked_add jobs through REST, and counts the requests and
response-body bytes each interface needs for three questions a client could ask:

  status_of_known_jobs     the state and result of N jobs whose ids the client holds
  jobs_with_tool_schema    the same plus the input schema of each job's tool
  succeeded_jobs_by_state  the caller's succeeded jobs, without knowing their ids

REST has no list route, so the third question cannot be answered at all; the first two take
one request per job (plus the whole OpenAPI document for the schema). Needs
requirements-optional-py313.lock (graphql-core).
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import socket
import sys
from threading import Thread
from time import monotonic, sleep

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import uvicorn  # noqa: E402

from vater_optional.graphql_api import Gateway, JobRecord  # noqa: E402
from vater_optional.graphql_api import create_app as graphql_app  # noqa: E402
from verified_execution.http_local import LOOPBACK  # noqa: E402
from verified_execution.http_local import create_app as rest_app  # noqa: E402
from verified_execution.tools import default_registry  # noqa: E402

TOKEN = "measurementOnlyCredential0123456789abcdef"


@contextmanager
def serve(app):
    sock = socket.socket()
    sock.bind((LOOPBACK, 0))
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


def call(port: int, method: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection(LOOPBACK, port, timeout=5)
    try:
        connection.request(method, path, body=None if body is None else json.dumps(body),
                           headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        reply = connection.getresponse()
        return reply.status, reply.read()
    finally:
        connection.close()


def measure(jobs: int = 20) -> dict:
    with serve(rest_app(TOKEN, None)) as port:
        ids = []
        for number in range(jobs):
            status, body = call(port, "POST", "/tools/checked_add/jobs", {"left": number, "right": 1})
            assert status == 202, body
            ids.append(json.loads(body)["job_id"])
        deadline = monotonic() + 10
        states = {}
        while monotonic() < deadline:  # wait until every job finished; these polls are not counted
            states = {job: json.loads(call(port, "GET", f"/jobs/{job}")[1]) for job in ids}
            if all(state["state"] == "succeeded" for state in states.values()):
                break
            sleep(0.02)
        status_bytes = sum(len(call(port, "GET", f"/jobs/{job}")[1]) for job in ids)
        openapi_bytes = len(call(port, "GET", "/openapi.json")[1])
    rest = {
        "status_of_known_jobs": {"requests": jobs, "response_bytes": status_bytes},
        "jobs_with_tool_schema": {"requests": jobs + 1, "response_bytes": status_bytes + openapi_bytes},
        "succeeded_jobs_by_state": {"requests": None, "response_bytes": None,
                                    "note": "no list route: the ids must already be known"},
    }
    records = [JobRecord(job, "local", "checked_add", states[job]["state"], states[job]["result"]) for job in ids]
    queries = {
        "status_of_known_jobs": "{ jobs(first: %d) { id state result } }" % jobs,
        "jobs_with_tool_schema": "{ jobs(first: %d) { id state result tool { name } } tools { name inputSchema } }"
                                 % jobs,
        "succeeded_jobs_by_state": "{ jobs(state: succeeded, first: %d) { id result } }" % jobs,
    }
    graphql = {}
    with serve(graphql_app(Gateway(records, default_registry()), {TOKEN: "local"})) as port:
        for name, query in queries.items():
            status, body = call(port, "POST", "/graphql", {"query": query})
            answer = json.loads(body)
            assert status == 200 and "errors" not in answer and len(answer["data"]["jobs"]) == jobs, answer
            graphql[name] = {"requests": 1, "response_bytes": len(body)}
    return {"jobs": jobs, "rest": rest, "graphql": graphql, "queries": queries}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=20)
    args = parser.parse_args()
    record = measure(args.jobs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({side: {k: v["requests"] for k, v in record[side].items()} for side in ("rest", "graphql")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
