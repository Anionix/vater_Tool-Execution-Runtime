"""Local-only subprocess integration harness: guarded JSON lines + durable ledger.

WIRE-TEST-001: This is NOT the MCP wire protocol; it is a reproducible test endpoint.
IDENTITY-001: The process supplies the principal, never the incoming JSON.
REPLAY-001: A repeated request returns ALREADY_RECORDED, and does not re-run.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .core import LocalAuthority
from .ledger import OperationLedger
from .local_cli import MAX_MESSAGE_BYTES, InputFormatError, reject_nonfinite, unique_fields
from .recorded_calculator import RecordedCalculator


def decode_recorded_request(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_MESSAGE_BYTES:
        raise InputFormatError("message too large")
    try:
        value = json.loads(raw.decode("utf-8"),
                           object_pairs_hook=unique_fields, parse_constant=reject_nonfinite)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise InputFormatError("invalid JSON") from exc
    if not isinstance(value, dict) or set(value) != {"request_key", "name", "arguments"}:
        raise InputFormatError("invalid request fields")
    if type(value["name"]) is not str or type(value["request_key"]) is not str:
        raise InputFormatError("invalid identifiers")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="LOCAL-ONLY recorded calculator")
    parser.add_argument("--database", required=True,
                        help="Dedicated writable local SQLite file")
    args = parser.parse_args()
    ledger = OperationLedger(args.database)
    service = RecordedCalculator(
        ledger, principal="local-process", authority=LocalAuthority.calculator_only())

    while True:
        raw = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
        if not raw:
            return 0
        try:
            request = decode_recorded_request(raw)
            result = service.execute(**request).to_dict()
        except InputFormatError:
            if len(raw) > MAX_MESSAGE_BYTES and not raw.endswith(b"\n"):
                # Same bounded memory pattern as the existing local test endpoint.
                while True:
                    chunk = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
                    if not chunk or chunk.endswith(b"\n"):
                        break
            result = {"ok": False, "value": None, "error_code": "INVALID_MESSAGE",
                      "operation_id": None, "ledger_state": None}
        sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
