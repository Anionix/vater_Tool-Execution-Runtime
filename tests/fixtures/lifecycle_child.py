"""Deterministic subprocess fixture; never use with untrusted input."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time

mode = sys.argv[1]
if mode == "flood":
    for _ in sys.stdin.buffer:
        for index in range(50000):
            payload = {"jsonrpc": "2.0", "id": index, "result": {"value": index}}
            sys.stdout.buffer.write((json.dumps(payload) + "\n").encode())
            sys.stdout.buffer.flush()
elif mode == "hung-after-eof":
    sys.stdin.buffer.read()
    time.sleep(60)
elif mode == "unexpected-exit":
    sys.exit(0)
elif mode == "grandchild":
    grandchild = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # TEST-FIXTURE-ATOMIC: publish a complete file; never expose an empty
    # destination path before the JSON has been fully written and closed.
    from pathlib import Path
    destination = Path(sys.argv[2])
    temporary = destination.with_suffix(destination.suffix + ".pending")
    temporary.write_text(json.dumps({"child": os.getpid(), "grandchild": grandchild.pid}), encoding="utf8")
    os.replace(temporary, destination)
    sys.stdin.buffer.read()
    time.sleep(60)
else:
    sys.exit(20)
