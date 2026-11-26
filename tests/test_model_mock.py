import pytest

from verified_execution.model_mock import (
    ModelContractError, ModelMessage, ModelRequest, ModelTemporarilyUnavailable,
    ModelText, OfflineResponses, ToolProposal,
)


REQUEST = ModelRequest((ModelMessage("developer", "Return a short answer."), ModelMessage("user", "2 + 2?")))


def test_fixture_preserves_message_order_and_keeps_proposals_inert():
    calls = []

    def fixture(payload):
        calls.append(payload)
        return {"output": [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "4"}]},
            {"type": "function_call", "name": "write_file", "arguments": '{"approved":true}', "call_id": "call_1"},
        ]}

    result = OfflineResponses(fixture).respond(REQUEST)
    assert calls == [{"model": "offline-responses-fixture", "input": [
        {"role": "developer", "content": "Return a short answer."},
        {"role": "user", "content": "2 + 2?"},
    ]}]
    assert result == (ModelText("4"), ToolProposal("write_file", '{"approved":true}', "call_1"))


@pytest.mark.parametrize("item", [
    {"type": "image_generation_call"},
    {"type": "message", "role": "assistant", "content": [{"type": "refusal"}]},
    {"type": "function_call", "name": "write_file", "arguments": {}, "call_id": "call_1"},
])
def test_unsupported_or_invalid_output_fails_explicitly(item):
    with pytest.raises(ModelContractError):
        OfflineResponses(lambda _: {"output": [item]}).respond(REQUEST)


def test_byte_limit_and_request_type_are_checked_before_the_fixture():
    def never_called(_):
        pytest.fail("fixture was called")

    adapter = OfflineResponses(never_called, max_input_bytes=16)
    with pytest.raises(ModelContractError, match="byte limit"):
        adapter.respond(REQUEST)
    with pytest.raises(ModelContractError, match="invalid model request"):
        adapter.respond({"name": "checked_add"})


def test_fixture_timeout_has_its_own_error(capsys):
    def unavailable(_):
        raise TimeoutError("private-token")

    with pytest.raises(ModelTemporarilyUnavailable, match="^model fixture timeout$"):
        OfflineResponses(unavailable).respond(REQUEST)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("task,root,items,result_reference", [
    ("006", "messages", "choices[0].message", "tool_call_id"),
    ("007", "messages", "content", "tool_use_id"),
    ("008", "contents", "candidates[0].content.parts", "functionResponse"),
])
def test_provider_format_design_examples(task, root, items, result_reference):
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "contracts/model_format_designs.json"
    design = json.loads(path.read_text())[f"task.provider_adapters.{task}"]
    request = design["text_request"]
    assert request[root]
    assert design["response_items_at"] == items
    assert design["tool_result_reference"] == result_reference
    assert design["status"] == ("deferred" if task == "008" else "design_only")
    if task == "006":
        assert [message["role"] for message in request[root]] == ["developer", "user"]
    elif task == "007":
        assert request["system"] and request["max_tokens"] > 0
        assert request[root][0]["content"][0]["type"] == "text"
    else:
        assert request["systemInstruction"]["parts"][0]["text"]
        assert request[root][0]["parts"][0]["text"]


def test_host_approval_and_common_authorization_are_both_required():
    from verified_execution.core import LocalAuthority
    from verified_execution.model_mock import execute_approved_proposal

    proposal = ToolProposal("checked_add", '{"left":2,"right":3}', "call_1")
    grant = LocalAuthority.calculator_only()
    different = ToolProposal("checked_add", '{"left":2,"right":4}', "call_1")
    for approval in (None, different):
        with pytest.raises(ModelContractError, match="host approval"):
            execute_approved_proposal(proposal, approved=approval, authority=grant)
    denied = execute_approved_proposal(proposal, approved=proposal, authority=LocalAuthority(frozenset()))
    assert denied.error_code == "NOT_AUTHORIZED"
    assert execute_approved_proposal(proposal, approved=proposal, authority=grant).result == 5
    write = ToolProposal("write_file", '{}', "call_2")
    assert execute_approved_proposal(write, approved=write, authority=grant).error_code == "UNKNOWN_TOOL"


def test_destination_and_budget_fail_before_fixture():
    from dataclasses import replace

    calls = []
    adapter = OfflineResponses(lambda payload: calls.append(payload) or {"output": []}, call_budget=1)
    for request in (replace(REQUEST, destination="remote"), replace(REQUEST, processing_region="other")):
        with pytest.raises(ModelContractError, match="destination"):
            adapter.respond(request)
    assert not calls
    assert adapter.respond(REQUEST) == ()
    with pytest.raises(ModelContractError, match="budget exhausted"):
        adapter.respond(REQUEST)
    assert len(calls) == 1


def test_fragment_order_and_end_are_checked():
    from verified_execution.model_mock import TextFragment, collect_fixture_stream

    first, last = TextFragment(0, "four"), TextFragment(1, "!", final=True)
    assert collect_fixture_stream(iter((first, last))) == ModelText("four!")
    for fragments in ((first,), (last, first), (first, first, last),
                      (first, TextFragment(2, "!", final=True)), (first, last, last)):
        with pytest.raises(ModelContractError):
            collect_fixture_stream(iter(fragments))
