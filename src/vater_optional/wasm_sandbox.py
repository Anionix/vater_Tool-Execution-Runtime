"""WebAssembly isolation prototype (task.optional_webassembly.*), on wasmtime 50.0.1.

Not part of the release: the shipped runtime executes no externally written code
(task.optional_webassembly.001), and this module exists to measure what isolating such code
would take. docs/OPTIONAL_PROTOTYPES.md records the decisions and the threat model.

A module runs only if every check passes, in this order:

  011  its SHA-256 is in the host's approved list (an unapproved module is never compiled),
       and it is a binary core module (b"\\0asm", version 1; no text, no precompiled code);
  010  it exports the vater-wasm-abi/1 interface with exactly these types:
         memory                      the guest's linear memory
         alloc(len: i32) -> i32      room for the input
         run(ptr: i32, len: i32) -> i64   output location, packed as (ptr << 32) | len
       and its input and output bytes follow the declared Interface (fixed-size little-endian
       fields, optionally one trailing byte string); a value of the wrong type or range is
       refused before the guest runs, and output of the wrong shape is a guest error;
  003  each import is a reviewed host function (HOST_FUNCTIONS) that this run grants, with
  009  exactly the reviewed type; nothing else is linked. No WASI is ever defined, so
  005  files, sockets, clocks, randomness and the environment are not reachable (004: no
  004  secret can be handed over by accident; the only data the guest sees is its input);
  006  compute is bounded by fuel, 007 memory, tables and instances by store limits, the stack
  007  by the engine, and 008 wall time by an epoch deadline.

Outcome.status separates the endings (008): completed, refused (nothing ran), guest_error,
fuel_exhausted, interrupted (deadline; no effectful host function had been called, so nothing
happened outside), and unknown: the run stopped abnormally after an effectful host call, so
the host cannot tell which effects the guest meant to finish. An unknown run must be
reconciled from the effects it did deliver, never retried blindly.

run_isolated() (012) runs the same checks in a child process with an empty environment,
no file writes and a CPU limit, for code that is not trusted enough to share the host process.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import subprocess
import sys
import threading
from typing import Callable, Mapping, Sequence

from wasmtime import (Config, Engine, Func, FuncType, Linker, MemoryType, Module, Store, Trap, TrapCode,
                      ValType, WasmtimeError)

ABI_VERSION = "vater-wasm-abi/1"
WASM_HEADER = b"\0asm\x01\0\0\0"  # a core module, binary format version 1
MAX_MODULE_BYTES = 1 << 20
MAX_INPUT_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_EMIT_BYTES = 4 * 1024
EPOCH_INTERVAL_S = 0.005
REQUIRED_EXPORTS = {"alloc": (("i32",), ("i32",)), "run": (("i32", "i32"), ("i64",))}
KINDS = {"u8": ("<B", 0, 2**8 - 1), "u32": ("<I", 0, 2**32 - 1),
         "u64": ("<Q", 0, 2**64 - 1), "i64": ("<q", -2**63, 2**63 - 1)}


@dataclass(frozen=True)
class Limits:
    fuel: int = 5_000_000             # 006: instructions, roughly; an endless loop ends here
    memory_bytes: int = 4 * 65536     # 007: linear memory, 4 pages of 64 KiB
    table_elements: int = 1_000
    timeout_s: float = 1.0            # 008: wall time, enforced by the epoch deadline
    stack_bytes: int = 256 * 1024     # deep recursion traps instead of exhausting the host


@dataclass(frozen=True)
class HostFunction:
    """One reviewed host function (009): what it can do, decided before any module may import it."""
    key: str
    module: str
    name: str
    params: tuple[str, ...]
    results: tuple[str, ...]
    effectful: bool
    side_effects: str
    review: str


HOST_FUNCTIONS = {
    "emit": HostFunction(
        "emit", "vater", "emit", ("i32", "i32"), ("i32",), effectful=True,
        side_effects=("Hands the bytes [ptr, ptr+len) of the guest's own memory (at most 4 KiB per call) to the "
                      "host's sink. The sink decides what the bytes cause; the guest reaches nothing else."),
        review=("Granted only to runs whose host supplies a sink. It is the one way out of the sandbox, so a run "
                "stopped after an emit is 'unknown', not 'interrupted'.")),
    "fail": HostFunction(
        "fail", "vater", "fail", ("i32",), (), effectful=False,
        side_effects="None. Ends the run as a guest error carrying the guest's code.",
        review="Lets a guest refuse its input explicitly; it reads and writes nothing."),
}
# 005 / 004: what is never linked, and why (documentation for the review; not linking is the control).
DENIED_IMPORTS = {
    "wasi_snapshot_preview1": "WASI: files, sockets, clocks, randomness, environment variables and arguments",
    "wasi_unstable": "older WASI: the same capabilities",
    "wasi": "WASI preview 2 interfaces",
    "env": "conventional C runtime imports (environment, process, memory management of the host)",
}
_BY_IMPORT = {(host.module, host.name): host for host in HOST_FUNCTIONS.values()}


class Refused(Exception):
    """A module or input that must not run; nothing was executed."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class InterfaceError(ValueError):
    """Bytes or values that do not fit the declared interface (010)."""


@dataclass(frozen=True)
class Field:
    name: str
    kind: str  # a key of KINDS, or "bytes" (only as the last field)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", self.name) or self.kind not in (*KINDS, "bytes"):
            raise InterfaceError(f"invalid field {self.name!r}: {self.kind!r}")


def _check_fields(fields: tuple[Field, ...]) -> None:
    names = [item.name for item in fields]
    if not fields or len(set(names)) != len(names):
        raise InterfaceError("fields must be present and uniquely named")
    if any(item.kind == "bytes" for item in fields[:-1]):
        raise InterfaceError("only the last field may be a byte string")


@dataclass(frozen=True)
class Interface:
    """The data types exchanged with one module (010): fixed little-endian fields."""
    input: tuple[Field, ...]
    output: tuple[Field, ...]

    def __post_init__(self) -> None:
        _check_fields(self.input)
        _check_fields(self.output)

    def encode(self, values: Mapping[str, object]) -> bytes:
        if not isinstance(values, Mapping) or set(values) != {item.name for item in self.input}:
            raise InterfaceError("the input must have exactly the declared fields")
        data = bytearray()
        for item in self.input:
            value = values[item.name]
            if item.kind == "bytes":
                if not isinstance(value, (bytes, bytearray)):
                    raise InterfaceError(f"{item.name}: a byte string is required")
                data += value
                continue
            fmt, low, high = KINDS[item.kind]
            if type(value) is not int or not low <= value <= high:  # bool and float are refused
                raise InterfaceError(f"{item.name}: an integer in [{low}, {high}] is required")
            data += struct.pack(fmt, value)
        if len(data) > MAX_INPUT_BYTES:
            raise InterfaceError("the input is larger than the interface allows")
        return bytes(data)

    def decode(self, data: bytes) -> dict[str, object]:
        fixed = sum(struct.calcsize(KINDS[item.kind][0]) for item in self.output if item.kind != "bytes")
        trailing = self.output[-1].kind == "bytes"
        if len(data) < fixed or (not trailing and len(data) != fixed):
            raise InterfaceError(f"the output has {len(data)} bytes; the interface expects "
                                 f"{'at least ' if trailing else ''}{fixed}")
        values: dict[str, object] = {}
        offset = 0
        for item in self.output:
            if item.kind == "bytes":
                values[item.name] = bytes(data[offset:])
                break
            fmt = KINDS[item.kind][0]
            (values[item.name],) = struct.unpack_from(fmt, data, offset)
            offset += struct.calcsize(fmt)
        return values


@dataclass(frozen=True)
class Outcome:
    status: str                    # completed | refused | guest_error | fuel_exhausted | interrupted | unknown
    reason: str = ""
    output: dict[str, object] | None = None
    granted: tuple[str, ...] = ()  # 009: the host functions this run was given, nothing more
    effects: int = 0               # effectful host calls made before the run ended
    emitted: tuple[bytes, ...] = ()
    fuel_used: int = 0


@dataclass(frozen=True)
class Loaded:
    digest: str
    label: str
    module: Module = field(repr=False)
    interface: Interface
    granted: tuple[str, ...]


def _types(function_type: FuncType) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return tuple(str(value) for value in function_type.params), tuple(str(value) for value in function_type.results)


class _GuestFailed(Exception):
    pass


class Sandbox:
    """Compiles approved modules and runs each call in a fresh, limited store."""

    def __init__(self, approved: Mapping[str, str], limits: Limits = Limits()) -> None:
        if not approved or not all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in approved):
            raise ValueError("approved modules are named by lower-case SHA-256 digests")
        self.approved, self.limits = dict(approved), limits
        config = Config()
        config.consume_fuel = True
        config.epoch_interruption = True
        config.max_wasm_stack = limits.stack_bytes
        config.wasm_threads = False      # no shared memory between guests or with the host
        config.wasm_memory64 = False
        self._engine = Engine(config)
        self._stop = threading.Event()
        self._ticker = threading.Thread(target=self._tick, name="wasm-epoch", daemon=True)
        self._ticker.start()

    def _tick(self) -> None:
        while not self._stop.wait(EPOCH_INTERVAL_S):
            self._engine.increment_epoch()

    def close(self) -> None:
        self._stop.set()
        self._ticker.join()

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def load(self, wasm: bytes, interface: Interface, grants: Sequence[str] = ()) -> Loaded:
        """011 / 010 / 003: approve, compile and check a module; raise Refused otherwise."""
        if not isinstance(wasm, (bytes, bytearray)) or len(wasm) > MAX_MODULE_BYTES:
            raise Refused("invalid_module", "a binary module of at most 1 MiB is required")
        digest = hashlib.sha256(wasm).hexdigest()
        if digest not in self.approved:
            raise Refused("unapproved_module", digest)  # never compiled, never started
        if bytes(wasm[:8]) != WASM_HEADER:
            raise Refused("invalid_module", "not a binary core WebAssembly module")
        unknown = set(grants) - set(HOST_FUNCTIONS)
        if unknown:
            raise Refused("unknown_grant", ", ".join(sorted(unknown)))
        try:
            module = Module(self._engine, bytes(wasm))
        except WasmtimeError as error:
            raise Refused("invalid_module", str(error).splitlines()[0]) from None
        exports = {item.name: item.type for item in module.exports}
        if not isinstance(exports.get("memory"), MemoryType):
            raise Refused("interface_mismatch", "memory is not exported")
        for name, expected in REQUIRED_EXPORTS.items():
            actual = exports.get(name)
            if not isinstance(actual, FuncType) or _types(actual) != expected:
                raise Refused("interface_mismatch", f"{name} must have the type {expected}")
        granted = []
        for item in module.imports:
            host = _BY_IMPORT.get((item.module, item.name))
            where = f"{item.module}.{item.name}"
            if host is None:
                raise Refused("import_not_allowed", where)
            if host.key not in grants:
                raise Refused("import_not_granted", where)
            if not isinstance(item.type, FuncType) or _types(item.type) != (host.params, host.results):
                raise Refused("import_type_mismatch", where)
            granted.append(host.key)
        return Loaded(digest, self.approved[digest], module, interface, tuple(sorted(set(granted))))

    def run(self, loaded: Loaded, values: Mapping[str, object],
            sink: Callable[[bytes], None] | None = None) -> Outcome:
        try:
            data = loaded.interface.encode(values)
        except InterfaceError as error:
            return Outcome("refused", f"invalid_input: {error}", granted=loaded.granted)
        limits = self.limits
        store = Store(self._engine)
        store.set_limits(memory_size=limits.memory_bytes, table_elements=limits.table_elements,
                         instances=1, tables=1, memories=1)
        store.set_fuel(limits.fuel)
        store.set_epoch_deadline(max(1, math.ceil(limits.timeout_s / EPOCH_INTERVAL_S)) + 1)
        effects, emitted = [0], []

        def emit(caller, pointer: int, length: int) -> int:
            memory = caller.get("memory")
            if not 0 <= length <= MAX_EMIT_BYTES or pointer < 0 or pointer + length > memory.data_len(caller):
                raise _GuestFailed("emit outside the guest's memory or over 4 KiB")
            chunk = bytes(memory.read(caller, pointer, pointer + length))
            effects[0] += 1  # counted before delivery: a failing sink may still have acted
            emitted.append(chunk)
            if sink is not None:
                sink(chunk)
            return 0

        def fail(code: int) -> None:
            raise _GuestFailed(f"the guest refused its input with code {code}")

        linker = Linker(self._engine)
        for key in loaded.granted:  # only what this module imports and this run grants
            host = HOST_FUNCTIONS[key]
            function_type = FuncType([getattr(ValType, kind)() for kind in host.params],
                                     [getattr(ValType, kind)() for kind in host.results])
            callback = {"emit": emit, "fail": fail}[key]
            linker.define(store, host.module, host.name,
                          Func(store, function_type, callback, access_caller=key == "emit"))

        def ended(status: str, reason: str, output: dict | None = None) -> Outcome:
            fuel_used = limits.fuel - store.get_fuel()
            if status != "completed" and effects[0]:
                status, reason = "unknown", f"{reason} after {effects[0]} effectful host call(s)"
            return Outcome(status, reason, output, loaded.granted, effects[0], tuple(emitted), fuel_used)

        try:
            try:
                instance = linker.instantiate(store, loaded.module)
            except WasmtimeError as error:
                return Outcome("refused", f"resource_limit: {str(error).splitlines()[0]}", granted=loaded.granted)
            exports = instance.exports(store)
            memory = exports["memory"]
            pointer = exports["alloc"](store, len(data))
            if pointer < 0 or pointer + len(data) > memory.data_len(store):
                return ended("guest_error", "alloc returned room outside the guest's memory")
            memory.write(store, data, pointer)
            packed = exports["run"](store, pointer, len(data)) & 0xFFFF_FFFF_FFFF_FFFF
            out_pointer, out_length = packed >> 32, packed & 0xFFFF_FFFF
            if out_length > MAX_OUTPUT_BYTES or out_pointer + out_length > memory.data_len(store):
                return ended("guest_error", "the output lies outside the guest's memory or is too large")
            raw = bytes(memory.read(store, out_pointer, out_pointer + out_length))
            return ended("completed", "", loaded.interface.decode(raw))
        except Trap as trap:
            if trap.trap_code == TrapCode.OUT_OF_FUEL:
                return ended("fuel_exhausted", "the compute limit was reached")
            if trap.trap_code == TrapCode.INTERRUPT:
                return ended("interrupted", "the time limit was reached")
            return ended("guest_error", f"trap: {trap.trap_code}")
        except _GuestFailed as error:
            return ended("guest_error", str(error))
        except InterfaceError as error:
            return ended("guest_error", f"malformed_output: {error}")
        except Exception as error:  # a failing sink: the effect may have happened
            return ended("guest_error", f"host_error: {type(error).__name__}")


# --- 012: the same sandbox behind a process boundary --------------------------------------------------------

def _hex_values(values: Mapping[str, object]) -> dict:
    return {key: {"hex": bytes(value).hex()} if isinstance(value, (bytes, bytearray)) else value
            for key, value in values.items()}


def _unhex_values(values: Mapping[str, object]) -> dict:
    return {key: bytes.fromhex(value["hex"]) if isinstance(value, dict) else value for key, value in values.items()}


_CHILD_LIMITS = """
import resource, sys
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))      # no file can be written
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
cpu = int(sys.argv[2])
resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
"""
_CHILD = _CHILD_LIMITS + """
sys.path.insert(0, sys.argv[1])
from vater_optional.wasm_sandbox import _child_main
_child_main()
"""


def _child_main() -> None:
    request = json.loads(sys.stdin.read())
    interface = Interface(tuple(Field(*item) for item in request["interface"]["input"]),
                          tuple(Field(*item) for item in request["interface"]["output"]))
    with Sandbox(request["approved"], Limits(**request["limits"])) as sandbox:
        try:
            loaded = sandbox.load(bytes.fromhex(request["wasm"]), interface, request["grants"])
            outcome = sandbox.run(loaded, _unhex_values(request["values"]))
        except Refused as error:
            outcome = Outcome("refused", str(error))
    print(json.dumps({"status": outcome.status, "reason": outcome.reason, "granted": list(outcome.granted),
                      "effects": outcome.effects, "emitted": [chunk.hex() for chunk in outcome.emitted],
                      "fuel_used": outcome.fuel_used,
                      "output": None if outcome.output is None else _hex_values(outcome.output)}))


def run_isolated(wasm: bytes, interface: Interface, values: Mapping[str, object], approved: Mapping[str, str],
                 grants: Sequence[str] = (), limits: Limits = Limits()) -> Outcome:
    """Run one call in a child process that holds no credentials and cannot write files."""
    request = {"wasm": bytes(wasm).hex(), "approved": dict(approved), "grants": list(grants),
               "interface": {"input": [[item.name, item.kind] for item in interface.input],
                             "output": [[item.name, item.kind] for item in interface.output]},
               "values": _hex_values(values), "limits": limits.__dict__}
    source = str(Path(__file__).resolve().parents[1])
    cpu_seconds = math.ceil(limits.timeout_s) + 2
    try:
        completed = subprocess.run([sys.executable, "-I", "-c", _CHILD, source, str(cpu_seconds)],
                                   input=json.dumps(request), capture_output=True, text=True, env={},
                                   timeout=limits.timeout_s + 30)
    except subprocess.TimeoutExpired:
        completed = None
    if completed is None or completed.returncode != 0:
        # The child died: whatever it emitted is lost with it, so an effectful grant makes this unknown.
        status = "unknown" if any(HOST_FUNCTIONS[key].effectful for key in grants) else "interrupted"
        return Outcome(status, "the isolated process ended without a result", granted=tuple(sorted(grants)))
    result = json.loads(completed.stdout)
    return Outcome(result["status"], result["reason"],
                   None if result["output"] is None else _unhex_values(result["output"]),
                   tuple(result["granted"]), result["effects"],
                   tuple(bytes.fromhex(chunk) for chunk in result["emitted"]), result["fuel_used"])
