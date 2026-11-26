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
