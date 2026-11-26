"""Additional side-effect-free arithmetic tools.

These tools are not part of the minimum release profile. A host opts in by
extending a registry, for example:

    registry = default_registry().with_tools(checked_multiply_spec())

Adding a tool here requires no change in any protocol adapter.

Machine contracts:
  INPUT-001: exactly left and right, each an exact unsigned 32-bit int.
  ARITHMETIC-002: multiplication returns OVERFLOW instead of wrapping.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .core import OperationError, UNSIGNED_32_MAX, require_unsigned_32, validated_arguments
from .tools import DEFAULT_CONTRACTS_DIR, ToolSpec


def checked_multiply(left: int, right: int) -> int:
    left = require_unsigned_32(left, "left")
    right = require_unsigned_32(right, "right")
    result = left * right
    if result > UNSIGNED_32_MAX:
        raise OperationError("OVERFLOW", "32-bit unsigned multiplication overflow")
    return result


def _run(operands: tuple[int, int]) -> int:
    return checked_multiply(*operands)


def _verify(operands: tuple[int, int], value: Any) -> None:
    # task.trust.007: re-derive the product independently of the returned value.
    left, right = (require_unsigned_32(operand, label) for operand, label in zip(operands, ("left", "right")))
    if type(value) is not int or not 0 <= value <= UNSIGNED_32_MAX or value != left * right:
        raise OperationError("INVALID_TOOL_RESULT", "invalid calculation result")


def checked_multiply_spec(contracts_dir: Path = DEFAULT_CONTRACTS_DIR) -> ToolSpec:
    def read(name: str) -> Any:
        return json.loads((contracts_dir / name).read_text(encoding="utf-8"))

    output_contract = read("checked_multiply.output.contract.json")
    success = output_contract["domains"]["success"]
    return ToolSpec(
        name="checked_multiply",
        description="Multiply unsigned 32-bit integers without overflow or side effects.",
        risk=read("tool_risks.json")["tools"]["checked_multiply"],
        input_schema=read("checked_multiply.input.schema.json"),
        output_schema={
            "type": success["type"],
            "properties": {"result": success["result"]},
            "required": success["required"],
            "additionalProperties": False,
        },
        validate=validated_arguments,
        run=_run,
        verify_result=_verify,
        contract_version=output_contract["contract_version"],
    )
