"""Test-only child that can flood output, stall its input, and spawn a descendant.

State paths: launched -> (read/flood | no_read | descendant) -> terminated.
No network, credentials, or access to external users.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import time

mode = sys.argv[1]
if mode == 'flood':
    for raw in sys.stdin.buffer:
        message = json.loads(raw)
        outgoing = {"jsonrpc":"2.0","id":message.get('id'),"result":{"value":"x"*3000}}
        sys.stdout.buffer.write((json.dumps(outgoing,separators=(',',':'))+'\n').encode())
        sys.stdout.buffer.flush()
elif mode == 'no_read':
    time.sleep(60)
elif mode in {'descendant', 'descendant_malformed'}:
    path = Path(sys.argv[2])
    descendant = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
    path.write_text(str(descendant.pid))
    if mode == 'descendant_malformed':
        sys.stdin.buffer.readline()
        sys.stdout.buffer.write(b'{"jsonrpc":"2.0","id":1,"result":NaN}\n')
        sys.stdout.buffer.flush()
    time.sleep(60)
elif mode == 'exit':
    sys.exit(0)
else:
    raise ValueError('unknown fixture mode')
