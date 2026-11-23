"""LIFECYCLE-001..004: real Linux pipes and OS process group tests.

Tests do not replace the official MCP Python SDK tests.
"""
from __future__ import annotations
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from guarded_stdio import DeadlinePipeWriter, WriteDeadlineExceeded

GUARD = str(ROOT / 'scripts' / 'guarded_stdio.py')
FIXTURE = str(ROOT / 'tests' / 'fixtures' / 'lifecycle_stdio_child.py')
VALID = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'


def _wrapper(*child: str, write_timeout: float = .2, shutdown_timeout: float = .6) -> list[str]:
    return [sys.executable, '-u', GUARD,
            '--write-timeout', str(write_timeout),
            '--shutdown-timeout', str(shutdown_timeout),
            '--', *child]


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.kill()
    try:
        proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


def _not_live(pid: int) -> bool:
    if os.name != 'posix':
        return True
    stat = Path(f'/proc/{pid}/stat')
    if stat.exists():
        return stat.read_text().split()[2] == 'Z'  # zombie cannot run
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True


def test_deadline_writer_accepts_writable_pipe():
    r, w = os.pipe()
    try:
        writer = DeadlinePipeWriter(w, write_timeout=.2)
        assert writer.write(b'complete\n') == len(b'complete\n')
        assert os.read(r, 20) == b'complete\n'
    finally:
        os.close(r)
        os.close(w)


def test_deadline_writer_rejects_blocked_reader():
    r, w = os.pipe()
    try:
        os.set_blocking(w, False)
        while True:
            try:
                os.write(w, b'x' * 4096)
            except BlockingIOError:
                break
        writer = DeadlinePipeWriter(w, write_timeout=.12)
        start = time.monotonic()
        with pytest.raises(WriteDeadlineExceeded):
            writer.write(b'{"jsonrpc":"2.0"}\n')
        assert .10 <= time.monotonic() - start < 1.0
    finally:
        os.close(r)
        os.close(w)


def test_real_stalled_reader_aborts_without_client_input_eof():
    proc = subprocess.Popen(_wrapper(sys.executable, '-u', FIXTURE, 'flood'),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0)
    assert proc.stdin and proc.stdout
    input_bytes = b''.join((b'{"jsonrpc":"2.0","id":%d,"method":"tools/list"}\n' % i)
                           for i in range(100))
    writer = threading.Thread(target=lambda: _send_best_effort(proc.stdin, input_bytes), daemon=True)
    try:
        writer.start()
        # Deliberately do not read the subprocess output.
        assert proc.wait(timeout=4) == 70
    finally:
        _stop(proc)


def _send_best_effort(pipe: io.BufferedIOBase, payload: bytes) -> None:
    try:
        pipe.write(payload)
        pipe.flush()
    except (BrokenPipeError, OSError):
        pass


def test_real_child_stalled_input_aborts_without_client_eof():
    proc = subprocess.Popen(_wrapper(sys.executable, '-u', FIXTURE, 'no_read'),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0)
    assert proc.stdin
    input_bytes = VALID * 4000  # Exceed typical OS pipe capacity (64 KiB).
    writer = threading.Thread(target=lambda: _send_best_effort(proc.stdin, input_bytes), daemon=True)
    try:
        writer.start()
        assert proc.wait(timeout=4) == 70
    finally:
        _stop(proc)


def test_disconnected_client_stdout_aborts_wrapper():
    proc = subprocess.Popen(_wrapper(sys.executable, '-u', str(ROOT/'tests'/'fixtures'/'echo_stdio_child.py')),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0)
    assert proc.stdin and proc.stdout
    try:
        proc.stdout.close()
        proc.stdin.write(VALID)
        proc.stdin.flush()
        assert proc.wait(timeout=4) == 70
    finally:
        _stop(proc)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX process groups only')
def test_sigterm_kills_child_and_descendant(tmp_path):
    marker = tmp_path / 'grandchild_pid'
    proc = subprocess.Popen(_wrapper(sys.executable, '-u', FIXTURE, 'descendant', str(marker)),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0)
    try:
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(.02)
        assert marker.exists(), 'descendant was not started'
        pid = int(marker.read_text())
        assert not _not_live(pid)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=4) == 128 + signal.SIGTERM
        for _ in range(100):
            if _not_live(pid):
                break
            time.sleep(.01)
        assert _not_live(pid), 'descendant was not terminated'
    finally:
        _stop(proc)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX process groups only')
def test_malformed_child_output_kills_descendant(tmp_path):
    marker = tmp_path / 'grandchild_pid'
    proc = subprocess.Popen(_wrapper(sys.executable, '-u', FIXTURE, 'descendant_malformed', str(marker)),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0)
    assert proc.stdin and proc.stdout
    try:
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(.02)
        assert marker.exists()
        pid = int(marker.read_text())
        proc.stdin.write(VALID)
        proc.stdin.flush()
        assert proc.wait(timeout=4) == 70
        assert proc.stdout.read() == b''
        for _ in range(100):
            if _not_live(pid):
                break
            time.sleep(.01)
        assert _not_live(pid)
    finally:
        _stop(proc)


def test_unexpected_child_exit_detected_even_with_idle_client_input():
    proc = subprocess.Popen(_wrapper(sys.executable, '-u', FIXTURE, 'exit'),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0)
    try:
        assert proc.wait(timeout=4) == 70
    finally:
        _stop(proc)


def test_child_ignoring_stdin_eof_is_terminated():
    proc = subprocess.run(_wrapper(sys.executable, '-u', FIXTURE, 'no_read'),
                          input=b'', capture_output=True, timeout=4)
    assert proc.returncode == 70


def test_rejects_invalid_write_and_shutdown_bounds():
    for option, value in [('--write-timeout', '-1'), ('--write-timeout', '31'),
                          ('--shutdown-timeout', '0'), ('--shutdown-timeout', '61')]:
        result = subprocess.run([sys.executable, GUARD, option, value,
                                 '--', sys.executable], capture_output=True, timeout=2)
        assert result.returncode != 0
