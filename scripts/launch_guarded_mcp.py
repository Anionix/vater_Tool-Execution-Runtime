"""Compatibility entry point for the fixed local release launcher."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def construct_command() -> list[str]:
    launcher = Path(__file__).resolve().with_name("run_local_mcp.py")
    return [sys.executable, "-I", str(launcher)]


def main() -> None:
    # task.trust.009: use the same argument checks and clean environment on both startup paths.
    command = construct_command()
    os.execve(command[0], [*command, *sys.argv[1:]], {})


if __name__ == "__main__":
    main()
