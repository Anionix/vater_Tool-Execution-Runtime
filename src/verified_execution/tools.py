"""Protocol-independent tool definitions.

A ToolSpec is the single place where a tool declares what it is: its name,
input/output contracts, risk class and trusted implementation. Protocol
adapters (MCP, HTTP, provider function calling, ...) translate these specs
into their own wire formats and must not redefine schemas or validation.

Machine contracts:
  REGISTRY-001: tool names are unique; a registry is immutable once built.
  REGISTRY-002: every tool declares one of the host-known risk classes.
  REGISTRY-003: schemas are copied from the host contract files, not adapters.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from functools import lru_cache
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping

from .core import OperationError, UNSIGNED_32_MAX, checked_add, require_unsigned_32, validated_arguments

RISK_CLASSES = frozenset({"read_only", "state_change", "irreversible", "code_execution"})
DEFAULT_CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"


def _frozen(value: Any) -> Any:
    """Deep read-only view so no adapter can mutate a shared contract."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_frozen(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    """Plain mutable copy for adapters whose libraries require dict/list."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return deepcopy(value)


@dataclass(frozen=True)
class ToolSpec:
    """Declaration of one trusted tool.

    validate: untrusted arguments -> trusted value, or raise OperationError.
    run: trusted value -> raw result, or raise OperationError.
    verify_result: (trusted value, raw result) -> None, or raise OperationError.
                   Called before any result leaves the runtime.
    """
    name: str
    description: str
    risk: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    validate: Callable[[Any], Any] = field(repr=False)
    run: Callable[[Any], Any] = field(repr=False)
    verify_result: Callable[[Any, Any], None] = field(repr=False)
    contract_version: str | None = None

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name or not self.name.isascii():
            raise ValueError("tool name must be a non-empty ASCII string")
        if self.risk not in RISK_CLASSES:
            raise ValueError(f"unknown risk class for {self.name}: {self.risk!r}")
        object.__setattr__(self, "input_schema", _frozen(self.input_schema))
        object.__setattr__(self, "output_schema", _frozen(self.output_schema))


class ToolRegistry:
    """Immutable, name-indexed set of ToolSpecs."""

    def __init__(self, specs: tuple[ToolSpec, ...] | list[ToolSpec]) -> None:
        indexed: dict[str, ToolSpec] = {}
        for spec in specs:
            if not isinstance(spec, ToolSpec):
                raise TypeError("registry accepts only ToolSpec")
            if spec.name in indexed:
                raise ValueError(f"duplicate tool name: {spec.name}")
            indexed[spec.name] = spec
        self._specs = MappingProxyType(indexed)

    def get(self, name: object) -> ToolSpec:
        spec = self._specs.get(name) if type(name) is str else None
        if spec is None:
            raise OperationError("UNKNOWN_TOOL", "tool is not registered")
        return spec

    def __contains__(self, name: object) -> bool:
        return type(name) is str and name in self._specs

    def __iter__(self) -> Iterator[ToolSpec]:
        return iter(self._specs.values())

    def names(self) -> frozenset[str]:
        return frozenset(self._specs)

    def with_tools(self, *specs: ToolSpec) -> "ToolRegistry":
        """New registry with extra tools; the original stays unchanged."""
        return ToolRegistry([*self, *specs])


# --- checked_add -----------------------------------------------------------

def _run_checked_add(operands: tuple[int, int]) -> int:
    return checked_add(*operands)


def _verify_checked_add(operands: tuple[int, int], value: Any) -> None:
    # task.trust.007: a well-formed but wrong value must not leave the runtime.
    left, right = operands
    left = require_unsigned_32(left, "left")
    right = require_unsigned_32(right, "right")
    if type(value) is not int or not 0 <= value <= UNSIGNED_32_MAX or value - left != right:
        raise OperationError("INVALID_TOOL_RESULT", "invalid calculation result")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def checked_add_spec(contracts_dir: Path = DEFAULT_CONTRACTS_DIR) -> ToolSpec:
    input_schema = _read_json(contracts_dir / "checked_add.input.schema.json")
    output_contract = _read_json(contracts_dir / "checked_add.output.contract.json")
    risks = _read_json(contracts_dir / "tool_risks.json")["tools"]
    success = output_contract["domains"]["success"]
    return ToolSpec(
        name="checked_add",
        description="Add unsigned 32-bit integers without overflow or side effects.",
        risk=risks["checked_add"],
        input_schema=input_schema,
        output_schema={
            "type": success["type"],
            "properties": {"result": success["result"]},
            "required": success["required"],
            "additionalProperties": False,
        },
        validate=validated_arguments,
        run=_run_checked_add,
        verify_result=_verify_checked_add,
        contract_version=output_contract["contract_version"],
    )


def load_registry(contracts_dir: Path = DEFAULT_CONTRACTS_DIR) -> ToolRegistry:
    """Registry of the host's minimum-profile tools, built from contract files."""
    return ToolRegistry([checked_add_spec(contracts_dir)])


@lru_cache(maxsize=1)
def default_registry() -> ToolRegistry:
    return load_registry()
