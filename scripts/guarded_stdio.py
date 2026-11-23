"""Optional bounded-input STDIO gate for an official MCP Python SDK server.

Contracts (security boundary, test separately from official SDK):
  WIRE-001: never buffer more than max_bytes+1 inbound bytes per line.
  WIRE-002: reject duplicated JSON member names at ANY nesting level.
  WIRE-003: reject non-finite JSON constants, invalid UTF-8, excessive depth.
  WIRE-004: do not change an accepted message's bytes.
  WIRE-005: preserve valid traffic from both legacy and modern protocol eras.
  WIRE-006: malformed request -> JSON-RPC parse error with null id;
            continue after bounded draining; fail closed for endless lines.
  OUTPUT-002: never forward an incomplete response line or corrupt JSON.
  OUTPUT-003: output lines have independent byte/depth budgets.
  OUTPUT-004: gateway-generated and child responses cannot interleave bytes.
  LIFECYCLE-001: stalled downstream output triggers bounded failure.
  LIFECYCLE-002: child and in-process-group descendants are cleaned on stop.
  LIFECYCLE-003: parent observes child failure and reacts even if input stalls.
  LIFECYCLE-004: total emitted output has a bounded byte budget.
  LIFECYCLE-005: stalled writes to child stdin abort within a deadline.

This is a local STDIO front gate, not a new MCP implementation. The child
server and the official SDK own JSON-RPC dispatch, versioning and tools.

State machine: START -> CHILD_READY -> READ -> (VALIDATED -> FORWARDED |
 REJECTED -> RESPONDED) -> READ -> EOF -> CLOSED. Unexpected child exit
 makes the gate fail closed. Do not use this as a security isolation sandbox.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from contextlib import nullcontext
from typing import Any, BinaryIO, Iterable

DEFAULT_MAX_BYTES = 4096
DEFAULT_MAX_DEPTH = 32
DEFAULT_MAX_OUTPUT_BYTES = 65536


class WireInputRejected(ValueError):
    pass


class WireOutputRejected(ValueError):
    """The child emitted malformed, oversized, or unterminated output."""


class OutputDeliveryFailed(OSError):
    """Downstream cannot accept a complete message within the configured budget."""


class WireStreamAborted(Exception):
    """An overlong line cannot be drained within the allowed resource budget."""
    pass


def _unique_json_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WireInputRejected("duplicate JSON member")
        result[key] = value
    return result


def _nonfinite(_value: str) -> None:
    raise WireInputRejected("non-finite JSON number")


def _check_json_depth(text: str, limit: int) -> None:
    """Count JSON structure nesting without confusing delimiters in strings."""
    depth = 0
    inside = False
    escape = False
    for char in text:
        if inside:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                inside = False
            continue
        if char == '"':
            inside = True
        elif char in "{[":
            depth += 1
            if depth > limit:
                raise WireInputRejected("JSON nesting too deep")
        elif char in "}]":
            depth -= 1
            if depth < 0:
                raise WireInputRejected("unexpected JSON delimiter")
    if depth != 0 or inside:
        raise WireInputRejected("incomplete JSON structure")


def validate_json_line(raw: bytes, *, max_bytes: int = DEFAULT_MAX_BYTES,
                       max_depth: int = DEFAULT_MAX_DEPTH) -> bytes:
    """Check a full NDJSON request while preserving exact accepted bytes."""
    if len(raw) > max_bytes:
        raise WireInputRejected("message too large")
    if not raw.endswith(b"\n"):
        raise WireInputRejected("missing newline delimiter")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise WireInputRejected("invalid UTF-8") from exc
    if text.startswith("\ufeff"):
        raise WireInputRejected("byte order mark forbidden")
    _check_json_depth(text, max_depth)
    try:
        obj = json.loads(text, object_pairs_hook=_unique_json_members, parse_constant=_nonfinite)
    except (ValueError, RecursionError) as exc:
        raise WireInputRejected("invalid JSON") from exc
    if not isinstance(obj, dict):
        raise WireInputRejected("JSON-RPC top level must be object")
    # The official SDK, not this guard, checks all method/era-specific fields.
    return raw


def _drain_through_newline(reader: BinaryIO, first: bytes, max_bytes: int) -> None:
    if first.endswith(b"\n"):
        return
    limit = max(65536, max_bytes * 16)
    drained = len(first)
    while drained <= limit:
        # Bounded chunk and bounded total; avoid infinite input draining.
        chunk = reader.readline(min(max_bytes + 1, limit - drained + 1))
        drained += len(chunk)
        if not chunk or chunk.endswith(b"\n"):
            return
    raise WireStreamAborted("oversize input exceeded bounded drain budget")


def _error_line() -> bytes:
    return b'{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"Parse error"}}\n'


def forward_valid_requests(reader: BinaryIO, child_writer: BinaryIO,
                           to_client: BinaryIO, *, max_bytes: int = DEFAULT_MAX_BYTES,
                           max_depth: int = DEFAULT_MAX_DEPTH,
                           output_lock: threading.Lock | None = None) -> dict[str, int]:
    """Purely incoming stream forwarding. Separate from child response pump."""
    counters = {"forwarded": 0, "rejected": 0}
    while True:
        line = reader.readline(max_bytes + 1)
        if not line:
            break
        try:
            validate_json_line(line, max_bytes=max_bytes, max_depth=max_depth)
        except WireInputRejected:
            if len(line) > max_bytes:
                _drain_through_newline(reader, line, max_bytes)
            with output_lock if output_lock is not None else nullcontext():
                to_client.write(_error_line())
                to_client.flush()
            counters["rejected"] += 1
            continue
        child_writer.write(line)
        child_writer.flush()
        counters["forwarded"] += 1
    return counters


def forward_child_responses(
    chunks: Iterable[bytes],
    destination: BinaryIO,
    *,
    max_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    output_lock: threading.Lock | None = None,
) -> int:
    """Output machine contract: BUFFER -> COMPLETE -> VALIDATED -> FORWARDED.

    A child response must be a single complete, bounded, valid JSON object line.
    Only complete lines are written, under the same lock as gateway error replies.
    A malformed/oversized/unterminated line is NEVER forwarded, even partially.
    This is a transport syntactic check, not MCP semantic validation.
    """
    pending = bytearray()
    count = 0
    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise WireOutputRejected("child output was not bytes")
        start = 0
        while start < len(chunk):
            end = chunk.find(b"\n", start)
            if end < 0:
                portion = chunk[start:]
                if len(pending) + len(portion) > max_bytes:
                    raise WireOutputRejected("child response exceeds limit")
                pending.extend(portion)
                break
            portion = chunk[start:end + 1]
            if len(pending) + len(portion) > max_bytes:
                raise WireOutputRejected("child response exceeds limit")
            pending.extend(portion)
            response = bytes(pending)
            try:
                validate_json_line(response, max_bytes=max_bytes, max_depth=max_depth)
            except WireInputRejected as exc:
                raise WireOutputRejected("child emitted invalid JSON response") from exc
            with output_lock if output_lock is not None else nullcontext():
                destination.write(response)
                destination.flush()
            count += 1
            pending.clear()
            start = end + 1
    if pending:
        raise WireOutputRejected("child response missing line terminator")
    return count



class InterruptibleFdReader:
    """Bounded buffered line reader whose blocking poll respects shutdown.

    This deliberately does NOT read sys.stdin.buffer in a daemon thread:
    its buffered lock can abort the Python interpreter during shutdown.
    Buffer state: EMPTY -> WAIT -> BUFFERED -> LINE_READY | EOF | STOP.
    """

    def __init__(self, fd: int, stop: threading.Event):
        self.fd = fd
        self.stop = stop
        self.pending = bytearray()
        self.eof = False

    def readline(self, size: int) -> bytes:
        if size <= 0:
            return b""
        while not self.stop.is_set():
            newline = self.pending.find(b"\n", 0, size)
            if newline >= 0:
                output = bytes(self.pending[:newline + 1])
                del self.pending[:newline + 1]
                return output
            if len(self.pending) >= size:
                output = bytes(self.pending[:size])
                del self.pending[:size]
                return output
            if self.eof:
                output = bytes(self.pending)
                self.pending.clear()
                return output
            ready, _, _ = select.select([self.fd], [], [], .05)
            if ready:
                data = os.read(self.fd, min(4096, size - len(self.pending)))
                if data:
                    self.pending.extend(data)
                else:
                    self.eof = True
        self.pending.clear()
        return b""


class DeadlineOutput:
    """Write serialized output without hanging forever on a slow pipe.

    State machine: OPEN -> WAIT_WRITABLE -> WRITING -> COMPLETE | ABORTED.
    No user-space queue is maintained. The kernel pipe supplies backpressure.
    Writes > PIPE_BUF may be partially delivered on failure; on failure the
    gateway aborts the connection, it never resumes a broken JSON frame.
    """

    def __init__(self, fd: int, *, timeout_seconds: float, max_total_bytes: int):
        self.fd = fd
        self.timeout_seconds = timeout_seconds
        self.max_total_bytes = max_total_bytes
        self.written = 0
        self.aborted = False
        os.set_blocking(fd, False)

    def write(self, data: bytes) -> int:
        if self.aborted:
            raise OutputDeliveryFailed("output connection already aborted")
        if self.written + len(data) > self.max_total_bytes:
            self.aborted = True
            raise OutputDeliveryFailed("total output budget exhausted")
        deadline = time.monotonic() + self.timeout_seconds
        offset = 0
        try:
            while offset < len(data):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OutputDeliveryFailed("downstream output stalled")
                _, ready, _ = select.select([], [self.fd], [], remaining)
                if not ready:
                    raise OutputDeliveryFailed("downstream output stalled")
                try:
                    count = os.write(self.fd, data[offset:])
                except BlockingIOError:
                    continue
                if count <= 0:
                    raise OutputDeliveryFailed("downstream output closed")
                offset += count
                self.written += count
        except OSError as exc:
            self.aborted = True
            if isinstance(exc, OutputDeliveryFailed):
                raise
            raise OutputDeliveryFailed("downstream write failed") from exc
        return offset

    def flush(self) -> None:
        # All writes bypass Python's buffering with os.write.
        return None


WriteDeadlineExceeded = OutputDeliveryFailed


class DeadlinePipeWriter(DeadlineOutput):
    """Convenience name for tests and external process boundary callers."""

    def __init__(self, fd: int, *, write_timeout: float):
        super().__init__(fd, timeout_seconds=write_timeout, max_total_bytes=1 << 30)


def _terminate_process_group(child: subprocess.Popen[bytes], grace: float = 0.3) -> None:
    """Best-effort cleanup of trusted descendants that keep the same group.

    Not a sandbox or a guarantee against children calling setsid()/setpgid().
    """
    if os.name == "posix":
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        # Even if the group leader exited, a grandchild may still be alive.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif child.poll() is None:
        child.terminate()
    try:
        child.wait(timeout=grace + 0.5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=1)


def _copy_parent_requests(
    src: BinaryIO, dst: BinaryIO, to_client: BinaryIO,
    faults: list[BaseException], stop: threading.Event,
    done: threading.Event, output_lock: threading.Lock,
    max_bytes: int, max_depth: int, child_stdin: BinaryIO,
) -> None:
    try:
        forward_valid_requests(src, dst, to_client, max_bytes=max_bytes,
                               max_depth=max_depth, output_lock=output_lock)
    except (BrokenPipeError, OSError, WireStreamAborted) as exc:
        faults.append(exc)
        stop.set()
    finally:
        # Closing child stdin communicates EOF. The fd is unbuffered.
        try:
            child_stdin.close()
        except (OSError, ValueError):
            pass
        done.set()


def _copy_child_stdout(
    src: BinaryIO,
    dst: BinaryIO,
    faults: list[BaseException],
    output_lock: threading.Lock,
    child: subprocess.Popen[bytes],
    max_bytes: int,
    max_depth: int,
    stop: threading.Event,
) -> None:
    try:
        def chunks():
            while True:
                data = os.read(src.fileno(), 4096)
                if not data:
                    break
                yield data
        forward_child_responses(chunks(), dst, max_bytes=max_bytes,
                                max_depth=max_depth, output_lock=output_lock)
    except (OSError, ValueError) as exc:
        faults.append(exc)
        # A child violating the output contract must not keep processing input.
        # This termination does not sandbox the child or protect against an
        # already-executed side effect.
        stop.set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded JSON guard for a child MCP stdio server")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    parser.add_argument("--max-output-bytes", type=int, default=DEFAULT_MAX_OUTPUT_BYTES)
    parser.add_argument("--max-output-depth", type=int, default=DEFAULT_MAX_DEPTH)
    parser.add_argument("--output-timeout", "--write-timeout", type=float, default=2.0)
    parser.add_argument("--input-write-timeout", type=float, default=None)
    parser.add_argument("--shutdown-timeout", type=float, default=4.0)
    parser.add_argument("--max-total-output-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--max-total-input-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if (not 128 <= args.max_bytes <= 1048576 or not 1 <= args.max_depth <= 256
            or not 128 <= args.max_output_bytes <= 16777216
            or not 1 <= args.max_output_depth <= 256
            or not .05 <= args.output_timeout <= 30.0
            or (args.input_write_timeout is not None
                and not .05 <= args.input_write_timeout <= 30.0)
            or not .05 <= args.shutdown_timeout <= 30.0
            or not args.max_output_bytes <= args.max_total_output_bytes <= (1 << 30)
            or not args.max_bytes <= args.max_total_input_bytes <= (1 << 30)):
        parser.error("invalid resource bounds")
    if os.name != "posix":
        parser.error("guarded process lifecycle currently requires POSIX pipes")
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command:
        parser.error("missing child server command")

    # Only trusted local children; this does not isolate untrusted code.
    child = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
        bufsize=0, start_new_session=(os.name == "posix"),
    )
    assert child.stdin is not None and child.stdout is not None
    output = DeadlineOutput(sys.stdout.buffer.fileno(),
                            timeout_seconds=args.output_timeout,
                            max_total_bytes=args.max_total_output_bytes)
    child_input = DeadlineOutput(child.stdin.fileno(),
                                 timeout_seconds=(args.input_write_timeout
                                                  if args.input_write_timeout is not None
                                                  else args.output_timeout),
                                 max_total_bytes=args.max_total_input_bytes)
    stop = threading.Event()
    input_done = threading.Event()
    faults: list[BaseException] = []
    output_lock = threading.Lock()

    pump = threading.Thread(
        target=_copy_child_stdout,
        args=(child.stdout, output, faults, output_lock, child,
              args.max_output_bytes, args.max_output_depth, stop),
        daemon=True, name="verified-output-pump",
    )
    receiver = threading.Thread(
        target=_copy_parent_requests,
        args=(InterruptibleFdReader(sys.stdin.buffer.fileno(), stop),
              child_input, output, faults, stop, input_done,
              output_lock, args.max_bytes, args.max_depth, child.stdin),
        daemon=True, name="verified-input-pump",
    )
    requested_signal: list[int] = []
    def request_stop(signum, _frame):
        requested_signal.append(signum)
        stop.set()

    previous_handlers = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)

    pump.start()
    receiver.start()
    status = 0
    last_input_done_at: float | None = None
    child_exited_at: float | None = None
    try:
        while True:
            if stop.is_set() or faults:
                status = 128 + requested_signal[0] if requested_signal else 70
                break

            child_code = child.poll()
            if input_done.is_set():
                if last_input_done_at is None:
                    last_input_done_at = time.monotonic()
                if child_code is None and time.monotonic() - last_input_done_at > args.shutdown_timeout:
                    status = 70
                    break
                if child_code is not None:
                    if child_exited_at is None:
                        child_exited_at = time.monotonic()
                    if not pump.is_alive():
                        if child_code != 0:
                            status = 70
                        break
                    if time.monotonic() - child_exited_at > args.shutdown_timeout:
                        status = 70
                        break
            else:
                # A dead child while an upstream client is still connected
                # must not leave this wrapper blocked forever in read().
                if child_code is not None or not pump.is_alive():
                    status = 70
                    break
            time.sleep(.02)
    finally:
        stop.set()
        _terminate_process_group(child)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        try:
            child.stdout.close()
        except OSError:
            pass
        pump.join(timeout=.5)
        receiver.join(timeout=.5)
        if faults:
            status = 70 if not requested_signal else status
    return status


if __name__ == "__main__":
    raise SystemExit(main())
