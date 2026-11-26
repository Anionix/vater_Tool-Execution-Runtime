"""LOCAL TEST HARNESS, NOT an MCP implementation.

A line-oriented JSON test endpoint. It exercises the exact core that the
separate official MCP SDK adapter uses. Do not expose over a network.
"""
from __future__ import annotations

import json
import sys
from typing import Any
from .json_bounds import check_json_depth
from .core import CalculationService, LocalAuthority

MAX_MESSAGE_BYTES = 4096


class InputFormatError(ValueError):
    pass


def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise InputFormatError("duplicate JSON member")
        out[key] = value
    return out


def reject_nonfinite(name: str) -> None:
    raise InputFormatError("non-finite numeric constant")


def decode_request_object(raw: bytes) -> Any:
    if len(raw) > MAX_MESSAGE_BYTES:
        raise InputFormatError("message too large")
    try:
        text = raw.decode("utf-8")
        check_json_depth(text, 32)
        return json.loads(text, object_pairs_hook=unique_fields, parse_constant=reject_nonfinite)
    except (ValueError, UnicodeError, TypeError) as exc:
        raise InputFormatError("invalid JSON") from exc


def decode_line(raw: bytes) -> dict[str, Any]:
    result = decode_request_object(raw)
    if not isinstance(result, dict) or set(result) != {"name", "arguments"}:
        raise InputFormatError("expected name and arguments")
    if not isinstance(result["name"], str):
        raise InputFormatError("name must be text")
    return result


def main() -> int:
    service = CalculationService(LocalAuthority.calculator_only())
    while True:
        # max-sized readline avoids unbounded memory consumption before rejecting.
        raw = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
        if not raw:
            return 0
        try:
            request = decode_line(raw)
            response = service.execute(request["name"], request["arguments"]).to_dict()
        except InputFormatError:
            # If the rejected line exceeded MAX_MESSAGE_BYTES, drain the remainder
            # before allowing subsequent requests to be processed.
            if len(raw) > MAX_MESSAGE_BYTES and not raw.endswith(b"\n"):
                while True:
                    chunk = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
                    if not chunk or chunk.endswith(b"\n"):
                        break
            response = {"ok": False, "value": None, "error_code": "INVALID_MESSAGE", "states": []}
        sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
