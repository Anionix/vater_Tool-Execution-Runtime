"""Offline fixture for the text subset of the OpenAI Responses contract."""

from dataclasses import dataclass
from collections.abc import Callable
import json
from typing import Literal


@dataclass(frozen=True)
class ModelMessage:
    role: Literal["system", "developer", "user", "assistant"]
    text: str


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[ModelMessage, ...]


@dataclass(frozen=True)
class ModelText:
    text: str


@dataclass(frozen=True)
class ToolProposal:
    name: str
    arguments: str
    call_id: str


class ModelContractError(ValueError):
    """The fixture supplied a response outside the supported subset."""


class ModelTemporarilyUnavailable(RuntimeError):
    """The selected fixture is temporarily unavailable."""


class OfflineResponses:
    """Run an injected local fixture, without provider selection or tool dispatch."""

    def __init__(self, fixture: Callable[[dict], dict], *, max_input_bytes: int = 16_384):
        if type(max_input_bytes) is not int or max_input_bytes <= 0:
            raise ValueError("invalid model input byte limit")
        self._fixture = fixture
        self._max_input_bytes = max_input_bytes

    def respond(self, request: ModelRequest) -> tuple[ModelText | ToolProposal, ...]:
        # task.provider_adapters.003: a typed model request cannot enter tool dispatch.
        if not isinstance(request, ModelRequest) or not request.messages:
            raise ModelContractError("invalid model request")
        messages = []
        for message in request.messages:
            if (not isinstance(message, ModelMessage)
                    or message.role not in ("system", "developer", "user", "assistant")
                    or not isinstance(message.text, str)):
                raise ModelContractError("invalid model message")
            messages.append({"role": message.role, "content": message.text})
        payload = {"model": "offline-responses-fixture", "input": messages}
        if len(json.dumps(payload, ensure_ascii=True).encode("ascii")) > self._max_input_bytes:
            raise ModelContractError("model input exceeds byte limit")
        try:
            response = self._fixture(payload)
        except TimeoutError:
            raise ModelTemporarilyUnavailable("model fixture timeout") from None
        return _read_response(response)


def _read_response(response: dict) -> tuple[ModelText | ToolProposal, ...]:
    if not isinstance(response, dict) or not isinstance(response.get("output"), list):
        raise ModelContractError("invalid model response")
    output = []
    for item in response["output"]:
        if not isinstance(item, dict):
            raise ModelContractError("invalid model output item")
        if item.get("type") == "message" and item.get("role") == "assistant":
            blocks = item.get("content")
            if not isinstance(blocks, list):
                raise ModelContractError("invalid model message")
            for block in blocks:
                if (not isinstance(block, dict) or block.get("type") != "output_text"
                        or not isinstance(block.get("text"), str)):
                    raise ModelContractError("unsupported model content block")
                output.append(ModelText(block["text"]))
        elif item.get("type") == "function_call":
            name, arguments, call_id = (item.get(key) for key in ("name", "arguments", "call_id"))
            if not isinstance(name, str) or not name or not isinstance(arguments, str):
                raise ModelContractError("invalid tool proposal")
            if not isinstance(call_id, str) or not call_id:
                raise ModelContractError("invalid tool proposal")
            # task.provider_adapters.010: proposals stay inert, even if their text claims authority.
            output.append(ToolProposal(name, arguments, call_id))
        else:
            raise ModelContractError("unsupported model output item")
    return tuple(output)
