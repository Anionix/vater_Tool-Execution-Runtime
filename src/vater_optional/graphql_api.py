"""GraphQL read interface prototype (task.optional_more_protocols.001-003), on graphql-core 3.3.2.

Not part of the release. docs/optional/MORE_PROTOCOLS.md records why (001: the queries REST
cannot serve in one request have no consumer yet) and what the measured difference is.

A query is answered only after every check passes, in this order:

  002  the request body is at most 4 KiB and the query at most 400 tokens; it parses and
       passes the standard GraphQL validation; then, counting through fragments, it is at
       most 5 fields deep, has at most 15 aliases, asks for at most 50 items per list, and
       its cost (1 per field, multiplied by the list size requested) is at most 300.
       A refused query runs no resolver.
  003  the principal comes from the host-issued credential, never from the query. Every job
       resolver returns only the principal's own jobs: another principal's job is
       indistinguishable from a job that does not exist, by id, in lists, through aliases
       and fragments. The owner is not a field, so no query can name another principal.

Only reads: the adapter calls nothing in the common core that changes state (task 012).
"""
from __future__ import annotations

from dataclasses import dataclass
import hmac
import json
from typing import Any, Iterable, Mapping

from graphql import (FieldNode, FragmentDefinitionNode, FragmentSpreadNode, GraphQLError, InlineFragmentNode,
                     IntValueNode, OperationDefinitionNode, VariableNode, build_schema, execute, parse,
                     specified_rules, validate)

from verified_execution.tools import thaw

SDL = '''
"The state of one job, as the local HTTP interface reports it."
enum JobState { prepared running succeeded failed cancelled }

type Tool {
  name: String!
  description: String!
  "The tool's input JSON Schema, serialized."
  inputSchema: String!
}

type Job {
  id: ID!
  state: JobState!
  result: Int
  error: String
  tool: Tool!
}

type Query {
  "One of the caller's own jobs; null for another principal's job and for an unknown id alike."
  job(id: ID!): Job
  "The caller's own jobs, newest first."
  jobs(state: JobState, first: Int = 10): [Job!]!
  tools: [Tool!]!
}
'''
SCHEMA = build_schema(SDL)
MAX_BODY_BYTES = 4096
MAX_TOKENS = 400
MAX_DEPTH = 5
MAX_ALIASES = 15
MAX_FIRST = 50
DEFAULT_FIRST = 10
MAX_COST = 300


@dataclass(frozen=True)
class JobRecord:
    id: str
    owner: str
    tool: str
    state: str
    result: int | None = None
    error: str | None = None


class LimitExceeded(GraphQLError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message, extensions={"code": code})


def _first(field: FieldNode, variables: Mapping[str, Any]) -> int:
    for argument in field.arguments or ():
        if argument.name.value != "first":
            continue
        if isinstance(argument.value, IntValueNode):
            return int(argument.value.value)
        if isinstance(argument.value, VariableNode):
            value = variables.get(argument.value.name.value, DEFAULT_FIRST)
            return value if type(value) is int else DEFAULT_FIRST
    return DEFAULT_FIRST if field.name.value == "jobs" else 1


def measure(document, variables: Mapping[str, Any] | None = None, operation_name: str | None = None) -> dict:
    """Depth, alias count, largest list size and cost of the operation that would run (002)."""
    variables = variables or {}
    fragments = {item.name.value: item for item in document.definitions if isinstance(item, FragmentDefinitionNode)}
    operations = [item for item in document.definitions if isinstance(item, OperationDefinitionNode)]
    chosen = [item for item in operations if operation_name is None or (item.name and item.name.value == operation_name)]
    totals = {"depth": 0, "aliases": 0, "largest_list": 0}

    def walk(selection_set, depth: int, seen: frozenset[str]) -> int:
        cost = 0
        for selection in selection_set.selections if selection_set else ():
            if isinstance(selection, FieldNode):
                totals["depth"] = max(totals["depth"], depth)
                totals["aliases"] += 1 if selection.alias else 0
                multiplier = _first(selection, variables)
                totals["largest_list"] = max(totals["largest_list"], abs(multiplier))
                cost += 1 + max(multiplier, 0) * walk(selection.selection_set, depth + 1, seen)
            elif isinstance(selection, InlineFragmentNode):
                cost += walk(selection.selection_set, depth, seen)
            elif isinstance(selection, FragmentSpreadNode):
                name = selection.name.value
                if name in fragments and name not in seen:  # cycles are already refused by validation
                    cost += walk(fragments[name].selection_set, depth, seen | {name})
        return cost

    totals["cost"] = sum(walk(operation.selection_set, 1, frozenset()) for operation in chosen[:1])
    return totals


def _check_limits(found: dict) -> None:
    if found["depth"] > MAX_DEPTH:
        raise LimitExceeded("QUERY_TOO_DEEP", f"depth {found['depth']} exceeds {MAX_DEPTH}")
    if found["aliases"] > MAX_ALIASES:
        raise LimitExceeded("TOO_MANY_ALIASES", f"{found['aliases']} aliases exceed {MAX_ALIASES}")
    if found["largest_list"] > MAX_FIRST:
        raise LimitExceeded("LIST_TOO_LARGE", f"lists are limited to {MAX_FIRST} items")
    if found["cost"] > MAX_COST:
        raise LimitExceeded("QUERY_TOO_COSTLY", f"cost {found['cost']} exceeds {MAX_COST}")


class Gateway:
    """Answers GraphQL reads over jobs and tools for one authenticated principal at a time."""

    def __init__(self, jobs: Iterable[JobRecord], registry: Iterable[Any]) -> None:
        self._jobs = {job.id: job for job in jobs}
        self._tools = {spec.name: spec for spec in registry}
        self.resolver_calls = 0

    def _tool(self, name: str) -> dict:
        spec = self._tools[name]
        return {"name": spec.name, "description": spec.description,
                "inputSchema": json.dumps(thaw(spec.input_schema), sort_keys=True, separators=(",", ":"))}

    def _view(self, job: JobRecord) -> dict:
        return {"id": job.id, "state": job.state, "result": job.result, "error": job.error,
                "tool": self._tool(job.tool)}

    def execute(self, principal: str, query: str, variables: Mapping[str, Any] | None = None,
                operation_name: str | None = None) -> dict:
        if not isinstance(principal, str) or not principal:
            raise ValueError("the host must name the authenticated principal")
        if not isinstance(query, str) or len(query.encode("utf-8")) > MAX_BODY_BYTES:
            return {"errors": [{"message": "the query is too large", "extensions": {"code": "QUERY_TOO_LARGE"}}]}
        if variables is not None and not isinstance(variables, Mapping):
            return {"errors": [{"message": "variables must be an object", "extensions": {"code": "BAD_REQUEST"}}]}
        try:
            document = parse(query, max_tokens=MAX_TOKENS)
        except GraphQLError as error:
            too_many = f"more than {MAX_TOKENS} tokens" in error.message
            code = "QUERY_TOO_LARGE" if too_many else "GRAPHQL_PARSE_FAILED"
            return {"errors": [{"message": error.message, "extensions": {"code": code}}]}
        problems = validate(SCHEMA, document, specified_rules)
        if problems:
            return {"errors": [{"message": problem.message, "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}}
                               for problem in problems]}
        try:
            _check_limits(measure(document, variables, operation_name))
        except LimitExceeded as error:
            return {"errors": [error.formatted]}
        gateway = self

        class Root:  # resolvers see only the principal's own jobs
            @staticmethod
            def job(info, id):
                gateway.resolver_calls += 1
                found = gateway._jobs.get(id)
                return gateway._view(found) if found is not None and found.owner == principal else None

            @staticmethod
            def jobs(info, first=DEFAULT_FIRST, state=None):
                gateway.resolver_calls += 1
                own = [job for job in gateway._jobs.values()
                       if job.owner == principal and (state is None or job.state == state)]
                return [gateway._view(job) for job in reversed(own)][:max(first, 0)]

            @staticmethod
            def tools(info):
                gateway.resolver_calls += 1
                return [gateway._tool(name) for name in sorted(gateway._tools)]

        result = execute(SCHEMA, document, root_value=Root(), variable_values=dict(variables or {}),
                         operation_name=operation_name)
        response: dict = {"data": result.data}
        if result.errors:
            response["errors"] = [error.formatted for error in result.errors]
        return response


def create_app(gateway: Gateway, credentials: Mapping[str, str]):
    """POST /graphql over Starlette. credentials maps each host-issued token to its principal."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    if not credentials or any(len(token) < 32 for token in credentials):
        raise ValueError("each credential must be at least 32 characters")

    def principal_of(request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return None
        offered = header[len("Bearer "):].encode()
        matched = None
        for token, principal in credentials.items():  # compare every token in constant time
            if hmac.compare_digest(offered, token.encode()):
                matched = principal
        return matched

    async def graphql(request: Request) -> JSONResponse:
        principal = principal_of(request)
        headers = {"Cache-Control": "no-store"}
        if principal is None:
            return JSONResponse({"errors": [{"message": "unauthorized", "extensions": {"code": "UNAUTHORIZED"}}]},
                                status_code=401, headers=headers)
        body = await request.body()
        try:
            if len(body) > MAX_BODY_BYTES:
                raise ValueError("too large")
            payload = json.loads(body)
            if not isinstance(payload, dict) or not isinstance(payload.get("query"), str):
                raise ValueError("no query")
        except ValueError:
            return JSONResponse({"errors": [{"message": "bad request", "extensions": {"code": "BAD_REQUEST"}}]},
                                status_code=400, headers=headers)
        answer = gateway.execute(principal, payload["query"], payload.get("variables"), payload.get("operationName"))
        return JSONResponse(answer, status_code=200 if "data" in answer else 400, headers=headers)

    return Starlette(routes=[Route("/graphql", graphql, methods=["POST"])])
