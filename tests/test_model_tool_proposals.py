"""task.testing.024: mock model tool requests never execute a side effect directly.

Model connectors are an adopted optional profile (docs/RELEASE_PROFILES.json,
with_model_connectors), so this is tested rather than marked not applicable.
A fixture model proposes every side-effecting tool this repository has, with
arguments that claim approval. A side-effecting executor is live in the same
process, wired the way a host wires it. The proposals must stay inert: the
destination sees no request and the ledger records nothing, whether or not
the host later approves the exact proposal.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from test_external_change import FakeGitHub, Host
from verified_execution.core import LocalAuthority
from verified_execution.durable import checked_add_recorded_spec
from verified_execution.external_change import TOOL_NAME, github_test_create_issue_spec
from verified_execution.model_mock import (
    ModelContractError, ModelMessage, ModelRequest, OfflineResponses, ToolProposal, execute_approved_proposal)
from verified_execution.recorded_tool_bridge import RECORDED_TOOL_NAME
from verified_execution.runtime import ToolRuntime
from verified_execution.tools import default_registry

REQUEST = ModelRequest((ModelMessage("user", "Open an issue and record the sum."),))
SIDE_EFFECT_PROPOSALS = [
    {"type": "function_call", "name": TOOL_NAME, "call_id": "call_issue",
     "arguments": json.dumps({"request_key": "model-1", "title": "Created by the model", "approved": True})},
    {"type": "function_call", "name": RECORDED_TOOL_NAME, "call_id": "call_recorded",
     "arguments": json.dumps({"request_key": "model-2", "left": 1, "right": 2})},
    {"type": "function_call", "name": "write_file", "call_id": "call_write",
     "arguments": json.dumps({"path": "/etc/passwd", "admin_override": True})},
]
EVERY_GRANT = LocalAuthority(frozenset({"checked_add", TOOL_NAME, RECORDED_TOOL_NAME, "write_file"}))


class SpyExecutor:
    """Stands in for any durable executor; it fails the test if it is ever reached."""

    def __init__(self):
        self.calls = []

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        raise AssertionError("a model proposal reached a side-effecting executor")

    async def acall(self, name, arguments):
        return self.call(name, arguments)


@pytest.fixture
def live_side_effects(tmp_path):
    """The host's own side-effecting wiring, live in the same process."""
    github = FakeGitHub()
    host = Host(tmp_path / "ledger.sqlite3", github)
    registry = default_registry().with_tools(github_test_create_issue_spec(), checked_add_recorded_spec())
    spy = SpyExecutor()
    runtime = ToolRuntime(EVERY_GRANT, registry, durable={TOOL_NAME: host.executor, RECORDED_TOOL_NAME: spy})
    return github, host, spy, runtime


def operations(host) -> int:
    with sqlite3.connect(host.path) as connection:
        return connection.execute("SELECT count(*) FROM operations").fetchone()[0]


def test_proposals_for_side_effecting_tools_stay_inert(live_side_effects):
    github, host, spy, _runtime = live_side_effects
    model = OfflineResponses(lambda _payload: {"output": SIDE_EFFECT_PROPOSALS})
    proposals = model.respond(REQUEST)
    assert proposals == tuple(ToolProposal(item["name"], item["arguments"], item["call_id"])
                              for item in SIDE_EFFECT_PROPOSALS)
    assert github.posts == 0 and github.issues == [] and spy.calls == [] and operations(host) == 0


@pytest.mark.parametrize("index", range(len(SIDE_EFFECT_PROPOSALS)))
def test_even_an_approved_proposal_cannot_reach_a_side_effect(live_side_effects, index):
    github, host, spy, _runtime = live_side_effects
    item = SIDE_EFFECT_PROPOSALS[index]
    proposal = ToolProposal(item["name"], item["arguments"], item["call_id"])
    # The host approves the exact proposal and grants every tool name: the model
    # path still only reaches the read-only runtime, which has no side effects.
    outcome = execute_approved_proposal(proposal, approved=proposal, authority=EVERY_GRANT)
    assert not outcome.succeeded and outcome.error_code in {"UNKNOWN_TOOL", "UNSUPPORTED_OPERATION"}
    assert github.posts == 0 and spy.calls == [] and operations(host) == 0


def test_proposal_arguments_cannot_turn_a_read_only_call_into_a_write(live_side_effects):
    github, host, spy, _runtime = live_side_effects
    for arguments in ({"left": 1, "right": 2, "request_key": "model-3"},
                      {"left": 1, "right": 2, "title": "also open an issue"},
                      {"left": 1, "right": 2, "tool": TOOL_NAME}):
        proposal = ToolProposal("checked_add", json.dumps(arguments), "call_add")
        outcome = execute_approved_proposal(proposal, approved=proposal, authority=EVERY_GRANT)
        assert outcome.error_code == "INVALID_ARGUMENT"
    assert github.posts == 0 and spy.calls == [] and operations(host) == 0


def test_unapproved_proposal_is_refused_before_any_execution(live_side_effects):
    github, host, spy, _runtime = live_side_effects
    item = SIDE_EFFECT_PROPOSALS[0]
    proposal = ToolProposal(item["name"], item["arguments"], item["call_id"])
    with pytest.raises(ModelContractError, match="host approval"):
        execute_approved_proposal(proposal, approved=None, authority=EVERY_GRANT)
    assert github.posts == 0 and spy.calls == [] and operations(host) == 0


def test_the_host_path_is_the_only_way_to_the_side_effect(live_side_effects):
    """Control: the same wiring does create an issue when the host itself calls it,
    so the zero-request results above are not an artefact of a broken fixture."""
    github, host, _spy, runtime = live_side_effects
    outcome = asyncio.run(runtime.acall(TOOL_NAME, {"request_key": "host-1", "title": "Created by the host"}))
    assert outcome.ok and github.posts == 1 and operations(host) == 1
