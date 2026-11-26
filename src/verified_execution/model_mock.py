"""Offline fixture for the text subset of the OpenAI Responses contract."""

from dataclasses import dataclass
from collections.abc import Callable, Iterable
import json
from threading import Lock
from typing import Literal

from .core import CalculationService, ExecutionResult, LocalAuthority
from .json_bounds import check_json_depth


@dataclass(frozen=True)
class ModelMessage:
    role: Literal["system", "developer", "user", "assistant"]
    text: str


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[ModelMessage, ...]
    destination: str = "local-fixture"
    processing_region: str = "host-local"


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

    def __init__(self, fixture: Callable[[dict], dict], *, max_input_bytes: int = 16_384,
                 call_budget: int | None = None):
        if type(max_input_bytes) is not int or max_input_bytes <= 0:
            raise ValueError("invalid model input byte limit")
        if call_budget is not None and (type(call_budget) is not int or call_budget < 0):
            raise ValueError("invalid model call budget")
        self._remaining_calls = call_budget
        self._budget_lock = Lock()
        self._fixture = fixture
        self._max_input_bytes = max_input_bytes

    def respond(self, request: ModelRequest) -> tuple[ModelText | ToolProposal, ...]:
        # task.provider_adapters.003: a typed model request cannot enter tool dispatch.
        if not isinstance(request, ModelRequest) or not request.messages:
            raise ModelContractError("invalid model request")
        # task.provider_adapters.015: fail closed before providing any data to the fixture.
        if (request.destination, request.processing_region) != ("local-fixture", "host-local"):
            raise ModelContractError("unsupported model destination or processing region")
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
        # task.provider_adapters.016: reserve one fixture call before entering the fixture.
        with self._budget_lock:
            if self._remaining_calls == 0:
                raise ModelContractError("model call budget exhausted")
            if self._remaining_calls is not None:
                self._remaining_calls -= 1
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

def execute_approved_proposal(
    proposal: ToolProposal, *, approved: ToolProposal | None, authority: LocalAuthority
) -> ExecutionResult:
    """Optional host-only entry for explicitly approved read-only calculator proposals."""
    # task.provider_adapters.011: approval must match the exact proposal; authorization is live.
    if approved is None or approved != proposal:
        raise ModelContractError("tool proposal needs explicit host approval")
    if len(proposal.arguments.encode("utf-8", errors="surrogatepass")) > 16_384:
        raise ModelContractError("tool proposal arguments exceed byte limit")

    def unique_fields(pairs):
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ModelContractError("duplicate tool proposal field")
        return fields

    try:
        check_json_depth(proposal.arguments, 16)
        arguments = json.loads(proposal.arguments, object_pairs_hook=unique_fields)
    except ValueError:
        raise ModelContractError("invalid tool proposal arguments") from None
    return CalculationService(authority).execute(proposal.name, arguments)


@dataclass(frozen=True)
class TextFragment:
    sequence: int
    text: str
    final: bool = False


def collect_fixture_stream(fragments: Iterable[TextFragment]) -> ModelText:
    """Collect the local fixture's numbered, bounded text fragments."""
    # task.provider_adapters.020: incomplete -> complete on the single final fragment.
    text_parts = []
    total_bytes = 0
    complete = False
    for expected, fragment in enumerate(fragments):
        if (complete or expected >= 1024 or type(fragment.sequence) is not int
                or fragment.sequence != expected or type(fragment.final) is not bool
                or not isinstance(fragment.text, str)):
            raise ModelContractError("invalid model stream sequence")
        total_bytes += len(fragment.text.encode("utf-8", errors="surrogatepass"))
        if total_bytes > 16_384:
            raise ModelContractError("model stream exceeds byte limit")
        text_parts.append(fragment.text)
        complete = fragment.final
    if not complete:
        raise ModelContractError("model stream ended before final fragment")
    return ModelText("".join(text_parts))
