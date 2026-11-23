"""SDK-independent tests of the byte boundary and the actual process wrapper."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from guarded_stdio import (WireInputRejected, forward_valid_requests,
                           validate_json_line, _error_line)

VALID = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n'


@pytest.mark.parametrize("payload", [
    b'{"a":1,"a":2}\n',
    b'{"a":{"x":0,"x":1}}\n',
    b'{"a":NaN}\n',
    b'{"a":Infinity}\n',
    b'\xff\n',
    b'\xef\xbb\xbf{}\n',
    b'[]\n',
    b'{}',
    b'{"a":2}\ntrailing\n',
    b'{"a":{"b":{"c":1}}}\n',  # depth 3 over artificial limit 2
])
def test_rejects_invalid_binary_and_structure(payload):
    with pytest.raises(WireInputRejected):
        validate_json_line(payload, max_depth=2)


def test_accepts_nested_text_with_escaped_braces_unchanged():
    raw = b'{"a":"hello [} \\" \\n", "b":{"c":1}}\n'
    assert validate_json_line(raw) == raw


def test_max_message_size_is_exact():
    message = b'{' + b' ' * 4092 + b'}\n'
    assert len(message) == 4095
    assert validate_json_line(message, max_bytes=4095) == message
    with pytest.raises(WireInputRejected):
        validate_json_line(message, max_bytes=4094)


def test_valid_legacy_and_modern_envelopes_unchanged():
    for m in [VALID, b'{"jsonrpc":"2.0","id":2,"method":"initialize","params":{}}\n',
              b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28"}}}\n']:
        assert validate_json_line(m) == m


def test_bounded_drain_and_recovery_in_memory():
    valid = VALID
    invalid = b'{"padding":"' + b'x' * 9000 + b'"}\n'
    source = io.BytesIO(valid + invalid + valid)
    sink = io.BytesIO()
    responses = io.BytesIO()
    result = forward_valid_requests(source, sink, responses, max_bytes=512)
    assert result == {"forwarded": 2, "rejected": 1}
    assert sink.getvalue() == valid + valid
    assert responses.getvalue() == _error_line()


def test_duplicate_keys_and_nonfinite_are_rejected_in_memory():
    source = io.BytesIO(b'{"a":1,"a":2}\n' + b'{"a":NaN}\n' + VALID)
    sink = io.BytesIO()
    reply = io.BytesIO()
    counters = forward_valid_requests(source, sink, reply)
    assert counters == {"forwarded":1, "rejected":2}
    assert sink.getvalue() == VALID
    assert reply.getvalue() == 2 * _error_line()


def test_real_subprocess_boundary_and_recovery():
    cmd = [sys.executable, "-u", str(ROOT / "scripts" / "guarded_stdio.py"),
           "--max-bytes", "512", "--", sys.executable, "-u",
           str(ROOT / "tests" / "fixtures" / "echo_stdio_child.py")]
    huge = b'{"data":"' + b'X'*5000 + b'"}\n'
    inputs = VALID + b'{"a":1,"a":2}\n' + huge + b'{"a":NaN}\n' + VALID
    proc = subprocess.run(cmd,input=inputs,capture_output=True,timeout=10)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    output = proc.stdout.splitlines(keepends=True)
    assert output.count(VALID) == 2
    assert output.count(_error_line()) == 3
    assert len(output) == 5  # response ordering may vary across concurrent pipes


def test_real_subprocess_invalid_utf8_does_not_desync():
    cmd = [sys.executable, "-u", str(ROOT / "scripts" / "guarded_stdio.py"),
           "--", sys.executable, "-u", str(ROOT / "tests" / "fixtures" / "echo_stdio_child.py")]
    proc = subprocess.run(cmd, input=b'\xff\n' + VALID, capture_output=True, timeout=10)
    assert proc.returncode == 0
    assert sorted(proc.stdout.splitlines()) == sorted((_error_line()+VALID).splitlines())


def test_missing_child_command_fails():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "guarded_stdio.py")],
                           capture_output=True, timeout=5)
    assert proc.returncode != 0


def test_invalid_config_fails():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "guarded_stdio.py"),
                           "--max-bytes", "10", "--", sys.executable],
                           capture_output=True, timeout=5)
    assert proc.returncode != 0


def test_unending_oversize_stream_fails_closed():
    cmd = [sys.executable, "-u", str(ROOT / "scripts" / "guarded_stdio.py"),
           "--max-bytes", "512", "--", sys.executable, "-u",
           str(ROOT / "tests" / "fixtures" / "echo_stdio_child.py")]
    proc = subprocess.run(cmd, input=b"z"*70000 + VALID,
                          capture_output=True, timeout=10)
    assert proc.returncode == 70  # do not continue after an undrainable line
    assert VALID not in proc.stdout


def test_child_failure_is_not_mistaken_for_success():
    cmd = [sys.executable, "-u", str(ROOT / "scripts" / "guarded_stdio.py"),
           "--", sys.executable, "-c", "import sys; sys.exit(5)"]
    proc = subprocess.run(cmd, input=b"", capture_output=True, timeout=10)
    assert proc.returncode == 70


def test_deterministic_generated_json_survives_boundary():
    """WIRE-007: deterministically generated UTF-8 JSON passes byte-for-byte."""
    import random
    randomizer = random.Random(20261123)
    alphabet = 'abc{}[]\\"\nこんにちは'
    for _ in range(1000):
        text = ''.join(randomizer.choice(alphabet) for _ in range(randomizer.randrange(0, 25)))
        obj = {"jsonrpc": "2.0", "id": randomizer.randint(0, 999999),
               "params": {"text": text}}
        payload = (json.dumps(obj, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
        assert validate_json_line(payload) == payload
