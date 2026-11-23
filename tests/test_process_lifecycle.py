"""LIFECYCLE-001..004: real pipe and descendant behavior on Linux/POSIX."""
from __future__ import annotations
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from guarded_stdio import DeadlineOutput, OutputDeliveryFailed

CHILD = ROOT / "tests" / "fixtures" / "lifecycle_child.py"
GUARD = ROOT / "scripts" / "guarded_stdio.py"
VALID = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'


def command(mode, *, options=(), tail=()):
    return [sys.executable, "-u", str(GUARD), *options, "--",
            sys.executable, "-u", str(CHILD), mode, *map(str, tail)]


@pytest.mark.skipif(os.name != "posix", reason="POSIX pipe semantics")
def test_nonblocking_output_stall_deadline_is_enforced():
    reader, writer = os.pipe()
    try:
        os.set_blocking(writer, False)
        while True:
            try:
                os.write(writer, b"a" * 4096)
            except BlockingIOError:
                break
        sink = DeadlineOutput(writer, timeout_seconds=.10, max_total_bytes=4096)
        started = time.monotonic()
        with pytest.raises(OutputDeliveryFailed, match="stalled"):
            sink.write(b"valid line\n")
        assert time.monotonic() - started < 1.0
        with pytest.raises(OutputDeliveryFailed, match="aborted"):
            sink.write(b"another\n")
    finally:
        os.close(reader)
        os.close(writer)


@pytest.mark.skipif(os.name != "posix", reason="POSIX pipe semantics")
def test_output_total_budget_is_enforced():
    r, w = os.pipe()
    try:
        sink = DeadlineOutput(w, timeout_seconds=.3, max_total_bytes=8)
        assert sink.write(b"1234") == 4
        assert sink.write(b"5678") == 4
        with pytest.raises(OutputDeliveryFailed, match="budget"):
            sink.write(b"9")
        assert os.read(r, 8) == b"12345678"
    finally:
        os.close(r)
        os.close(w)


@pytest.mark.skipif(os.name != "posix", reason="POSIX pipes and process groups")
def test_slow_client_triggers_failure_instead_of_deadlock():
    proc = subprocess.Popen(command("flood", options=("--output-timeout", ".2")),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    assert proc.stdin is not None
    assert proc.stdout is not None
    try:
        proc.stdin.write(VALID)
        proc.stdin.close()
        # Deliberately do not read stdout so its kernel pipe fills up.
        assert proc.wait(timeout=5) == 70
    finally:
        proc.kill() if proc.poll() is None else None
        proc.wait(timeout=2)
        proc.stdout.close()
        proc.stderr.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX pipes and process groups")
def test_client_disconnection_causes_child_cleanup():
    proc = subprocess.Popen(command("flood", options=("--output-timeout", ".2")),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write(VALID)
        proc.stdin.flush()
        proc.stdout.close()
        proc.stdin.close()
        assert proc.wait(timeout=5) == 70
    finally:
        proc.kill() if proc.poll() is None else None
        proc.wait(timeout=2)
        proc.stderr.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_child_hanging_after_eof_is_stopped():
    result = subprocess.run(command("hung-after-eof", options=("--shutdown-timeout", ".15")),
                            input=VALID, capture_output=True, timeout=5)
    assert result.returncode == 70


def test_child_exit_is_seen_without_parent_eof():
    proc = subprocess.Popen(command("unexpected-exit"), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # The parent deliberately keeps upstream stdin open.
        assert proc.wait(timeout=4) == 70
    finally:
        proc.kill() if proc.poll() is None else None
        proc.wait(timeout=2)
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists() and stat.read_text().split(") ", 1)[1].startswith("Z"):
        return False  # zombie is already terminated, awaiting reaping
    return True


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process verification")
def test_sigterm_cleans_child_and_grandchild(tmp_path):
    pid_path = tmp_path / "pids.json"
    proc = subprocess.Popen(command("grandchild", tail=(pid_path,)),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 3
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert pid_path.exists(), "grandchild did not start"
        pids = json.loads(pid_path.read_text())
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 128 + signal.SIGTERM
        deadline = time.monotonic() + 2
        while any(_is_running(v) for v in pids.values()) and time.monotonic() < deadline:
            time.sleep(.03)
        assert all(not _is_running(v) for v in pids.values()), pids
    finally:
        # TEST-FIXTURE-CLEANUP: a failed assertion must not orphan a subprocess.
        # The fixture starts a new process group whose leader is the child PID.
        if pid_path.exists() and pid_path.stat().st_size:
            try:
                data = json.loads(pid_path.read_text())
                leader = int(data["child"])
                if os.getpgid(leader) == leader:
                    os.killpg(leader, signal.SIGKILL)
            except (ValueError, KeyError, ProcessLookupError):
                pass
        proc.kill() if proc.poll() is None else None
        proc.wait(timeout=2)
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_budget_caps_total_child_responses():
    result = subprocess.run(
        command("flood", options=("--max-output-bytes", "256",
                                   "--max-total-output-bytes", "256")),
        input=VALID, capture_output=True, timeout=5,
    )
    assert result.returncode == 70
    assert 0 < len(result.stdout) <= 256
    for row in result.stdout.splitlines():
        assert json.loads(row)["jsonrpc"] == "2.0"
