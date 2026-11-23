import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from verified_execution.local_cli import InputFormatError, decode_line, MAX_MESSAGE_BYTES

ROOT = Path(__file__).resolve().parents[1]


def run_cli(lines):
    proc = subprocess.run([sys.executable, "-m", "verified_execution.local_cli"],
        input=b"".join(lines), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, timeout=5)
    assert proc.returncode == 0, proc.stderr.decode()
    return [json.loads(line) for line in proc.stdout.splitlines()]


@pytest.mark.parametrize("raw", [
    b'{"name":"checked_add","arguments":{"left":1,"left":2,"right":3}}\n',
    b'{"name":"checked_add","arguments":{"left":NaN,"right":3}}\n',
    b'{"name":"checked_add","arguments":{"left":Infinity,"right":3}}\n',
    b'{"name":"checked_add","arguments":{"left":1,"right":2}} extra\n',
    b'{"name":"checked_add","arguments":{"left":1,"right":2},"name":"checked_add"}\n',
    b'{"name":"checked_add","arguments":{"left":1,"right":2}}\xff\n',
    b'[]\n', b'{}\n',
])
def test_malformed_json_rejected(raw):
    with pytest.raises(InputFormatError):
        decode_line(raw)


def test_oversize_rejected_before_json_parsing():
    with pytest.raises(InputFormatError):
        decode_line(b"x" * (MAX_MESSAGE_BYTES+1))


def test_real_subprocess_good_bad_and_recovery():
    lines = [
        b'{"name":"checked_add","arguments":{"left":100,"right":200}}\n',
        b'{"name":"checked_add","arguments":{"left":4294967295,"right":1}}\n',
        b'{"name":"checked_add","arguments":{"left":true,"right":1}}\n',
        b'{"name":"checked_add","arguments":{"left":2,"right":3}}\n',
        b'{"name":"delete_file","arguments":{}}\n',
    ]
    results = run_cli(lines)
    assert [r["error_code"] for r in results] == [None, "OVERFLOW", "INVALID_ARGUMENT", None, "UNKNOWN_TOOL"]
    assert [r["value"] for r in results] == [300, None, None, 5, None]


def test_large_line_drained_then_recovers():
    big = b'{"name":"checked_add","arguments":{"left":1,"right":2},"padding":"' + b'0'*8192 + b'"}\n'
    results = run_cli([big, b'{"name":"checked_add","arguments":{"left":1,"right":2}}\n'])
    assert len(results) == 2
    assert results[0]["error_code"] == "INVALID_MESSAGE"
    assert results[1]["value"] == 3


def test_duplicate_json_member_rejected_end_to_end():
    results = run_cli([b'{"name":"checked_add","arguments":{"left":1,"left":2,"right":3}}\n'])
    assert results[0]["error_code"] == "INVALID_MESSAGE"
