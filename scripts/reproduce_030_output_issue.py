"""Negative control: demonstrate the previous 0.3.0 output-mixing defect.

Runs only when the immediately preceding local version is available.
Not a formal proof. This reproduces the exact previous _copy_child_stdout.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
OLD = ROOT.parent / 'python_first_mcp_secure_stdio_2026-11-23' / 'scripts' / 'guarded_stdio.py'
if not OLD.is_file():
    raise SystemExit('Old version source unavailable. Negative control not executed.')

spec = importlib.util.spec_from_file_location('guarded_stdio_previous', OLD)
assert spec and spec.loader
old = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old)

response = b'{"jsonrpc":"2.0","id":1,"result":{"result":3}}\n'
invalid_reply = old._error_line()
read_fd, write_fd = os.pipe()
output = io.BytesIO()
lock = threading.Lock()
errors = []
reader = os.fdopen(read_fd,'rb',buffering=0)
thread = threading.Thread(target=old._copy_child_stdout,args=(reader,output,errors,lock))
thread.start()
try:
    os.write(write_fd,response[:18])
    deadline = time.monotonic()+2
    while time.monotonic() < deadline and output.getvalue() != response[:18]:
        time.sleep(0.001)
    if output.getvalue() != response[:18]:
        raise AssertionError('Old child pump did not flush a partial response as expected')
    with lock:
        output.write(invalid_reply)
    os.write(write_fd,response[18:])
finally:
    os.close(write_fd)
thread.join(timeout=2)
reader.close()
assert not thread.is_alive()
assert not errors, errors
lines = output.getvalue().splitlines()
malformed = sum(1 for line in lines if not _is_valid_json(line)) if False else None

def is_valid_json(line: bytes) -> bool:
    try:
        json.loads(line)
        return True
    except (ValueError,UnicodeDecodeError):
        return False

malformed = sum(not is_valid_json(x) for x in lines)
assert malformed > 0, output.getvalue()
report = {'previous_version':'0.3.0','old_response_mixing_reproduced':True,
          'fragmented_output':True,'malformed_json_lines':malformed,
          'output_length_bytes':len(output.getvalue()),
          'status':'negative_control_success'}
result = ROOT / 'evidence' / 'previous_output_mixing_reproduction.json'
result.write_text(json.dumps(report,indent=2) + '\n', encoding='utf-8')
print(json.dumps(report))
