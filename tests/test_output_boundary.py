"""SDK-independent verification of OUTPUT-002, OUTPUT-003, OUTPUT-004.

All tests exercise actual Python source, not the official MCP SDK.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from guarded_stdio import (WireOutputRejected, _error_line, forward_child_responses)

RESPONSE = b'{"jsonrpc":"2.0","id":1,"result":{"result":3}}\n'


def test_split_line_reassembled_without_modification():
    chunks = [RESPONSE[:3], RESPONSE[3:15], RESPONSE[15:-1], RESPONSE[-1:]]
    sink = io.BytesIO()
    assert forward_child_responses(chunks, sink) == 1
    assert sink.getvalue() == RESPONSE


def test_multiple_responses_in_one_or_many_chunks():
    sink = io.BytesIO()
    assert forward_child_responses([RESPONSE + RESPONSE[:20], RESPONSE[20:]], sink) == 2
    assert sink.getvalue() == RESPONSE * 2


def test_rejection_is_atomic_with_pending_child_response():
    sink = io.BytesIO()
    lock = threading.Lock()

    def interleaved_chunks():
        yield RESPONSE[:19]  # A partial child response has arrived.
        with lock:
            sink.write(_error_line())  # An unrelated bad client request arrives.
        yield RESPONSE[19:]

    assert forward_child_responses(interleaved_chunks(), sink, output_lock=lock) == 1
    # No mixture of bytes; results can be in either request order by design.
    assert sink.getvalue() == _error_line() + RESPONSE
    for line in sink.getvalue().splitlines():
        assert isinstance(json.loads(line), dict)


def test_oversized_output_rejected_before_any_forward():
    sink = io.BytesIO()
    invalid = b'{"result":"' + b'x' * 600 + b'"}\n'
    with pytest.raises(WireOutputRejected, match="exceeds limit"):
        forward_child_responses([invalid[:180], invalid[180:]], sink, max_bytes=256)
    assert sink.getvalue() == b''


def test_unterminated_child_response_never_leaks():
    sink = io.BytesIO()
    with pytest.raises(WireOutputRejected, match="missing line terminator"):
        forward_child_responses([RESPONSE[:-1]], sink)
    assert sink.getvalue() == b''


@pytest.mark.parametrize('bad', [
    b'{"a":1,"a":2}\n',
    b'{"a":{"x":1,"x":2}}\n',
    b'{"result":NaN}\n',
    b'\xff\n',
    b'[]\n',
    b'{not-json}\n',
    b'{}\n{}',
])
def test_invalid_child_response_fails_closed(bad):
    sink = io.BytesIO()
    with pytest.raises(WireOutputRejected):
        forward_child_responses([bad], sink)
    # When one valid line preceded a malicious later line, earlier writes
    # cannot be undone. These cases are intentionally malformed at start.
    assert sink.getvalue() == b'' if bad != b'{}\n{}' else sink.getvalue() == b'{}\n'


def test_exact_boundary_on_response_size():
    sink = io.BytesIO()
    assert forward_child_responses([RESPONSE], sink, max_bytes=len(RESPONSE)) == 1
    with pytest.raises(WireOutputRejected):
        forward_child_responses([RESPONSE], io.BytesIO(), max_bytes=len(RESPONSE)-1)


def test_depth_bound_output():
    with pytest.raises(WireOutputRejected):
        forward_child_responses([b'{"a":{"b":{"c":1}}}\n'], io.BytesIO(), max_depth=2)


def test_malicious_child_stdout_causes_real_wrapper_failure():
    cmd = [sys.executable, "-u", str(ROOT / "scripts" / "guarded_stdio.py"),
           "--", sys.executable, "-u", str(ROOT / "tests" / "fixtures" / "evil_stdio_child.py")]
    valid = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
    proc = subprocess.run(cmd, input=valid, capture_output=True, timeout=12)
    assert proc.returncode == 70, proc.stderr.decode(errors="replace")
    assert proc.stdout == b''


def test_config_rejects_invalid_output_limit():
    cmd = [sys.executable, str(ROOT / "scripts" / "guarded_stdio.py"),
           "--max-output-bytes", "10", "--", sys.executable]
    proc = subprocess.run(cmd, capture_output=True, timeout=5)
    assert proc.returncode != 0


def test_wrapper_rejects_large_child_response():
    cmd = [sys.executable, "-u", str(ROOT / "scripts" / "guarded_stdio.py"),
           "--max-output-bytes", "256", "--", sys.executable, "-u",
           str(ROOT / "tests" / "fixtures" / "huge_stdio_child.py")]
    valid = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
    proc = subprocess.run(cmd, input=valid, capture_output=True, timeout=12)
    assert proc.returncode == 70, proc.stderr.decode(errors="replace")
    assert proc.stdout == b''
