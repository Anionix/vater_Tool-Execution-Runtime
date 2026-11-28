"""task.optional_more_protocols.004-008: gRPC, Protocol Buffers, Agent2Agent and editor connections.

Decisions recorded in docs/optional/MORE_PROTOCOLS.md, checked against the code they describe:
every inter-process channel stays on one host (004) and one executor serves every entry point
(006), so neither gRPC nor Agent2Agent is introduced; the .proto compatibility test (005) and
the delegation rules (007) are executable; the target editors connect through MCP (008).
Standard library only.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from vater_optional.delegation import DelegationRefused, Grant, approve, delegate, spend
from vater_optional.proto_compat import Unsupported, breaking_changes, parse

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "optional" / "MORE_PROTOCOLS.md").read_text(encoding="utf-8")
LOCKS = [(ROOT / name).read_text(encoding="utf-8").lower()
         for name in ("requirements-py313.lock", "requirements-optional-py313.lock")]


def section(task: str) -> str:
    return DOC.split(f"（task.optional_more_protocols.{task}）", 1)[1].split("\n## ", 1)[0]


def test_every_inter_process_channel_stays_on_one_host_and_grpc_is_not_introduced():
    """004: the channels that exist are listed with their code; none crosses hosts, so no gRPC."""
    text = section("004")
    files = re.findall(r"`((?:src/verified_execution/)?[a-z_]+\.py)`", text)
    assert sorted(files) == sorted(["server_mcp.py", "src/verified_execution/http_local.py",
                                    "src/verified_execution/worker_leases.py",
                                    "src/verified_execution/recorded_cli.py"])
    for name in files:
        assert (ROOT / name).is_file(), name
    assert "127.0.0.1" in (ROOT / "src/verified_execution/http_local.py").read_text(encoding="utf-8")
    assert "gRPC は導入しない" in text
    for lock in LOCKS:
        assert "grpcio" not in lock and "protobuf" not in lock
    for source in [*(ROOT / "src").rglob("*.py"), ROOT / "server_mcp.py"]:
        assert not re.search(r"^\s*(import|from)\s+grpc", source.read_text(encoding="utf-8"), re.M), source


OLD = """
syntax = "proto3";
// v1 of the job status message
message JobStatus {
  string id = 1;
  string state = 2;
  optional int64 result = 3;
  repeated string warnings = 4;
  reserved 9;
  reserved "legacy";
}
message Tool { string name = 1; }
"""


def test_adding_fields_and_retiring_them_properly_is_compatible():
    """005: new numbers are compatible; a removal that reserves number and name keeps old readers working."""
    added = OLD.replace("reserved 9;", "reserved 9;\n  string error = 5;")
    assert breaking_changes(OLD, added) == []
    retired = OLD.replace('repeated string warnings = 4;\n  reserved 9;\n  reserved "legacy";',
                          'reserved 4, 9;\n  reserved "legacy", "warnings";')
    assert breaking_changes(OLD, retired) == []
    assert set(parse(OLD)["JobStatus"]["fields"]) == {1, 2, 3, 4}


@pytest.mark.parametrize("change, problem", [
    (("optional int64 result = 3;", "optional int32 result = 3;"), "type changed"),
    (("repeated string warnings = 4;", "string warnings = 4;"), "cardinality changed"),
    (("string state = 2;", "string status = 2;"), "renamed"),
    (("repeated string warnings = 4;", ""), "removed without reserving"),
    (("repeated string warnings = 4;", "reserved 4;"), "removed without reserving"),
    (("reserved 9;", "reserved 9;\n  string again = 9;"), "reuses a reserved number"),
    (('reserved "legacy";', 'reserved "legacy";\n  string legacy = 10;'), "reuses a reserved name"),
    (("message Tool { string name = 1; }", ""), "message was removed"),
], ids=["type", "cardinality", "rename", "removed", "number_only_reserved", "reserved_number",
        "reserved_name", "message"])
def test_a_change_that_breaks_an_old_field_is_refused(change, problem):
    """005: every way of breaking a field of the previous version is reported."""
    found = breaking_changes(OLD, OLD.replace(*change))
    assert found and any(problem in item for item in found), found


@pytest.mark.parametrize("construct", [
    "message A { message B { string x = 1; } }", "message A { oneof x { string y = 1; } }",
    "message A { map<string, string> x = 1; }", "enum E { A = 0; }", 'import "other.proto";',
    "message A { required string x = 1; }", "message A { string x = 1; int32 y = 1; }",
    "message A { string x; }",
], ids=["nested", "oneof", "map", "enum", "import", "required", "duplicate_number", "no_number"])
def test_what_the_check_cannot_judge_is_refused_not_skipped(construct):
    with pytest.raises(Unsupported):
        parse(construct)


def test_one_executor_serves_every_entry_point_so_agent2agent_is_not_introduced():
    """006: MCP, local HTTP and the local line interface all call the same runtime on one host."""
    for path, marker in (("server_mcp.py", "ToolRuntime"), ("src/verified_execution/http_local.py", "ToolRuntime"),
                         ("src/verified_execution/local_cli.py", "CalculationService")):
        assert marker in (ROOT / path).read_text(encoding="utf-8"), path
    from verified_execution.core import CalculationService
    from verified_execution.runtime import ToolRuntime
    assert CalculationService is ToolRuntime
    text = section("006")
    assert "独立した実行主体が複数存在しない" in text and "Agent2Agent は導入しない" in text
    for lock in LOCKS:
        assert "a2a" not in re.findall(r"^([a-z0-9-]+)==", lock, re.M)


NOW = 1_000_000.0


def root_grant(**changes) -> Grant:
    base = dict(principal="planner", root="alice", tools=frozenset({"checked_add", "create_issue"}),
                budget_cents=1_000, expires_at=NOW + 7200, may_delegate=True)
    return Grant(**{**base, **changes})


@pytest.mark.parametrize("arguments, code", [
    (dict(tools={"*"}), "TOOLS_NOT_EXPLICIT"),
    (dict(tools=set()), "TOOLS_NOT_EXPLICIT"),
    (dict(tools={"checked_add", "delete_repository"}), "TOOLS_EXCEED_PARENT"),
    (dict(budget_cents=0), "BUDGET_NOT_BOUNDED"),
    (dict(budget_cents=10**9), "BUDGET_EXCEEDS_PARENT"),
    (dict(budget_cents=1.5), "BUDGET_NOT_BOUNDED"),
    (dict(expires_at=NOW + 10**6), "EXPIRY_NOT_BOUNDED"),
    (dict(expires_at=NOW + 3601), "EXPIRY_NOT_BOUNDED"),
    (dict(to="alice"), "INVALID_DELEGATE"),
], ids=["wildcard", "empty", "wider_tools", "zero_budget", "unlimited_budget", "fractional_budget",
        "past_parent_expiry", "over_an_hour", "to_root"])
def test_a_delegate_is_never_given_unbounded_authority(arguments, code):
    """007: tools, budget and time are always explicit and inside the parent's."""
    request = dict(to="researcher", tools={"checked_add"}, budget_cents=100, expires_at=NOW + 600, now=NOW)
    with pytest.raises(DelegationRefused) as refused:
        delegate(root_grant(), **{**request, **arguments})
    assert refused.value.code == code


def test_delegated_budget_is_held_back_and_chains_are_bounded():
    child, parent = delegate(root_grant(), "researcher", {"checked_add"}, 400, NOW + 600, NOW, may_delegate=True)
    assert parent.budget_cents == 600 and child.budget_cents == 400 and child.depth == 1 and child.root == "alice"
    child = spend(child, "checked_add", 300, NOW + 1, needs_approval=False)
    with pytest.raises(DelegationRefused) as exhausted:
        spend(child, "checked_add", 101, NOW + 2, needs_approval=False)
    assert exhausted.value.code == "BUDGET_EXHAUSTED"
    with pytest.raises(DelegationRefused) as outside:
        spend(child, "create_issue", 1, NOW + 2, needs_approval=False)
    assert outside.value.code == "TOOL_NOT_GRANTED"
    with pytest.raises(DelegationRefused) as late:
        spend(child, "checked_add", 1, NOW + 601, needs_approval=False)
    assert late.value.code == "GRANT_EXPIRED"
    with pytest.raises(DelegationRefused) as deeper:
        delegate(child, "helper", {"checked_add"}, 50, NOW + 300, NOW, may_delegate=True)
    assert deeper.value.code == "CHAIN_TOO_LONG"
    leaf, _ = delegate(child, "helper", {"checked_add"}, 50, NOW + 300, NOW)
    with pytest.raises(DelegationRefused) as again:
        delegate(leaf, "another", {"checked_add"}, 10, NOW + 100, NOW)
    assert again.value.code == "DELEGATION_NOT_ALLOWED"
    with pytest.raises(DelegationRefused) as expired:
        delegate(root_grant(expires_at=NOW), "researcher", {"checked_add"}, 1, NOW + 1, NOW)
    assert expired.value.code == "PARENT_EXPIRED"


def test_approvals_never_travel_with_a_delegation():
    """007: a tool needing approval must be approved for the delegate by the root human principal."""
    parent = approve(root_grant(), "create_issue", "alice")
    child, _ = delegate(parent, "researcher", {"create_issue"}, 100, NOW + 600, NOW)
    assert child.approved_tools == frozenset()
    with pytest.raises(DelegationRefused) as refused:
        spend(child, "create_issue", 10, NOW + 1, needs_approval=True)
    assert refused.value.code == "APPROVAL_REQUIRED"
    for approver in ("planner", "researcher", "attacker"):  # neither agent nor a message can approve
        with pytest.raises(DelegationRefused) as wrong:
            approve(child, "create_issue", approver)
        assert wrong.value.code == "APPROVER_NOT_ROOT"
    approved = approve(child, "create_issue", "alice")
    assert spend(approved, "create_issue", 10, NOW + 1, needs_approval=True).budget_cents == 90


def test_the_target_editors_are_decided_and_connect_through_mcp():
    """008: the editors are named, and the MCP server they use passes the official tool scenarios."""
    text = section("008")
    assert "Claude Code" in text and "Visual Studio Code" in text and "調査も実装もしない" in text
    results = json.loads((ROOT / "evidence" / "mcp-conformance" / "results.json").read_text(encoding="utf-8"))
    assert results["executed"] is True and results["verification_status"] == "passed"
    latest = results["versions"][max(results["versions"])]
    for scenario in ("tools-list", "tools-call-simple-text", "tools-call-error"):
        assert latest[scenario][0] == [scenario, "SUCCESS"], scenario
        assert f"`{scenario}`" in text
    assert (ROOT / "server_mcp.py").is_file()
