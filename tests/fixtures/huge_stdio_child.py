"""Oversized child output fixture, intentionally invalid for wrapper contract."""
import sys
for line in sys.stdin.buffer:
    sys.stdout.buffer.write(b'{"result":"' + b'x' * 8000 + b'"}\n')
    sys.stdout.buffer.flush()
