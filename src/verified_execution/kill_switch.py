"""task.operations.015: an operator kill switch for tools with external effects.

A KillSwitch is a file whose existence means "stop". It is passed to
LocalAuthority as its revocation event, so every grant check consults it:
before an operation is reserved, in the ledger immediately before dispatch
(the last point before a request leaves the process), and again before any
resend after a rate-limit wait. Once observed, it stays engaged for the life
of the process (removing the file re-enables tools only after a restart),
and an unreadable path counts as engaged.

    authority = LocalAuthority(frozenset({...}), revocation_event=KillSwitch.from_env())
"""
from __future__ import annotations

import os
from pathlib import Path
import threading

ENV_VAR = "VATER_KILL_SWITCH_FILE"


def default_path() -> Path:
    configured = os.environ.get(ENV_VAR)
    return Path(configured) if configured else Path.home() / ".vater" / "kill-switch"


class KillSwitch:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._engaged = threading.Event()

    @classmethod
    def from_env(cls) -> "KillSwitch":
        return cls(default_path())

    def is_set(self) -> bool:
        """Duck-types threading.Event.is_set for LocalAuthority.revocation_event."""
        if self._engaged.is_set():
            return True
        try:
            present = self.path.exists()
        except OSError:
            present = True  # fail closed: a path that cannot be checked stops tools
        if present:
            self._engaged.set()
        return present

    def engage(self, reason: str = "") -> None:
        self._engaged.set()  # this process stops even if the file cannot be written
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(reason + "\n", encoding="utf-8")
