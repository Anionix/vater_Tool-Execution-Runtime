"""Validation at the receiving end of the local checked_add protocol."""
from __future__ import annotations

from .core import OperationError, UNSIGNED_32_MAX, require_unsigned_32


def read_checked_add_result(*, left: int, right: int, is_error: bool,
                            structured_content: object) -> int:
    """Return only a verified integer. Free-form response text is never consumed."""
    # task.trust.007: received -> shape checked -> matched to original operands -> returned.
    left = require_unsigned_32(left, "left")
    right = require_unsigned_32(right, "right")
    if is_error is not False or type(structured_content) is not dict or set(structured_content) != {"result"}:
        raise OperationError("INVALID_TOOL_RESULT", "invalid calculation response")
    value = structured_content["result"]
    if type(value) is not int or not 0 <= value <= UNSIGNED_32_MAX or value - left != right:
        raise OperationError("INVALID_TOOL_RESULT", "invalid calculation result")
    return value
