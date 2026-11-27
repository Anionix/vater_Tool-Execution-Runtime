#!/usr/bin/env python3
"""task.operations.015: engage, inspect or clear the external-tool kill switch.

  python scripts/kill_switch.py engage --reason "incident 42"
  python scripts/kill_switch.py status
  python scripts/kill_switch.py clear      # takes effect for newly started processes only

The file is $VATER_KILL_SWITCH_FILE (default ~/.vater/kill-switch); see
docs/runbooks/disable_external_tools.md.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from verified_execution.kill_switch import KillSwitch, default_path  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["engage", "status", "clear"])
    parser.add_argument("--path", type=Path, default=None)
    parser.add_argument("--reason", default="")
    args = parser.parse_args(argv)
    path = args.path or default_path()
    if args.action == "engage":
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        KillSwitch(path).engage(f"{stamp} {args.reason}".strip())
    elif args.action == "clear":
        path.unlink(missing_ok=True)
    engaged = KillSwitch(path).is_set()
    print(json.dumps({"path": str(path), "engaged": engaged}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
