"""Fixture ONLY: echoes validated JSON lines, not a real MCP server."""
import sys
for item in sys.stdin.buffer:
    sys.stdout.buffer.write(item)
    sys.stdout.buffer.flush()
