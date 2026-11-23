"""Demonstrate OUTPUT-004 for the fixed 0.4.0 child response pump."""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
script=ROOT / 'scripts' / 'guarded_stdio.py'
spec=importlib.util.spec_from_file_location('guarded_stdio_fixed',script)
assert spec and spec.loader
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class FinishedChild:
    def poll(self):
        return 0
    def terminate(self):
        raise AssertionError('no termination expected')

read_fd,write_fd=os.pipe()
reader=os.fdopen(read_fd,'rb',buffering=0)
reply=b'{"jsonrpc":"2.0","id":1,"result":{"result":3}}\n'
sink=io.BytesIO()
lock=threading.Lock()
faults=[]
worker=threading.Thread(target=mod._copy_child_stdout,
 args=(reader,sink,faults,lock,FinishedChild(),65536,32))
worker.start()
try:
    os.write(write_fd,reply[:18])
    time.sleep(0.04)
    assert sink.getvalue()==b'', 'partial child JSON was forwarded'
    with lock:
        sink.write(mod._error_line())
    os.write(write_fd,reply[18:])
finally:
    os.close(write_fd)
worker.join(timeout=3)
reader.close()
assert not worker.is_alive()
assert not faults,faults
lines=sink.getvalue().splitlines()
assert len(lines)==2
assert all(isinstance(json.loads(line),dict) for line in lines)
assert sink.getvalue() == mod._error_line() + reply
record={'fixed_version':'0.4.0','response_atomicity_verified':True,
        'partial_response_forwarded':False,'valid_output_lines':len(lines),
        'status':'positive_control_success'}
path=ROOT/'evidence'/'fixed_output_atomicity_reproduction.json'
path.write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
print(json.dumps(record))
