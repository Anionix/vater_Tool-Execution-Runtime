"""Malicious or broken test fixture: emits invalid JSON, never a real SDK."""
import sys
for line in sys.stdin.buffer:
    sys.stdout.buffer.write(b'{"jsonrpc":"2.0","id":1,"id":2,"result":{}}\n')
    sys.stdout.buffer.flush()
