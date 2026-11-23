"""Host-only launcher for the initial official MCP stdio server.

STATE: POLICY_LOADED -> COMMAND_CONSTRUCTED -> EXEC_GATEWAY -> SDK_SERVER.
No untrusted argument controls the path to the SDK or any limit.
This launcher cannot run until official MCP SDK v2 is installed.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]

def construct_command() -> list[str]:
    policy = json.loads((ROOT / "config" / "limits.json").read_text(encoding="utf-8"))
    for name in ("request_input_max_bytes", "request_output_max_bytes", "json_max_depth"):
        if type(policy.get(name)) is not int or not 1 <= policy[name] <= 65536:
            raise ValueError("INVALID_HOST_POLICY:" + name)
    return [sys.executable, "-u", str(ROOT / "scripts" / "guarded_stdio.py"),
            "--max-bytes", str(policy["request_input_max_bytes"]),
            "--max-depth", str(policy["json_max_depth"]),
            "--max-output-bytes", str(policy["request_output_max_bytes"]),
            "--", sys.executable, "-u", str(ROOT / "server_mcp.py")]


def main() -> None:
    argv = construct_command()
    # Never print anything to stdout; it is reserved for MCP protocol bytes.
    os.execv(sys.executable, argv)

if __name__ == "__main__":
    main()
