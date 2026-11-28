"""task.optional_webassembly.001-014: the WebAssembly isolation prototype (src/vater_optional/wasm_sandbox.py).

Runs only with requirements-optional-py313.lock (wasmtime 50.0.1); the base jobs skip this
module. Each test drives real guests compiled from WebAssembly text: approved and unapproved
modules, every import the sandbox refuses, endless loops, memory and table bombs, deep
recursion, out-of-bounds output, memory dumps that look for host secrets, and runs cut off
before and after an effectful host call.
"""
from __future__ import annotations

import hashlib
from importlib import util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import pytest

wasmtime = pytest.importorskip("wasmtime")
from wasmtime import wat2wasm  # noqa: E402

from vater_optional import wasm_sandbox  # noqa: E402
from vater_optional.wasm_sandbox import (HOST_FUNCTIONS, Field, Interface, InterfaceError, Limits,  # noqa: E402
                                         Refused, Sandbox, run_isolated)

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "optional" / "WEBASSEMBLY.md").read_text(encoding="utf-8")
CHECKED_ADD = wat2wasm((ROOT / "tests" / "fixtures" / "wasm" / "checked_add.wat").read_text(encoding="utf-8"))
APPROVED = json.loads((ROOT / "tests" / "fixtures" / "wasm" / "approved.json").read_text(encoding="utf-8"))["approved"]
ADD = Interface((Field("left", "u32"), Field("right", "u32")), (Field("status", "u8"), Field("sum", "u32")))
BYTES = Interface((Field("data", "bytes"),), (Field("data", "bytes"),))
WORD = Interface((Field("data", "bytes"),), (Field("value", "u32"),))
ECHO_RESULT = "(i64.or (i64.shl (i64.extend_i32_u (local.get $ptr)) (i64.const 32)) (i64.extend_i32_u (local.get $len)))"
WORD_RESULT = "(i64.or (i64.shl (i64.const 512) (i64.const 32)) (i64.const 4))"
EMIT = '(import "vater" "emit" (func $emit (param i32 i32) (result i32)))'
FAIL = '(import "vater" "fail" (func $fail (param i32)))'


def guest(body: str, imports: str = "", memory: str = '(memory (export "memory") 1)', extra: str = "",
          run_type: str = "(param $ptr i32) (param $len i32) (result i64)") -> bytes:
    return wat2wasm(f"""(module {imports} {memory} {extra}
      (global $next (mut i32) (i32.const 1024))
      (func (export "alloc") (param $len i32) (result i32) (local $start i32)
        (local.set $start (global.get $next))
        (global.set $next (i32.add (global.get $next) (local.get $len))) (local.get $start))
      (func (export "run") {run_type} {body}))""")


ECHO = guest(ECHO_RESULT)
DUMP = guest("(i64.const 65536)")  # the whole first page: ptr 0, len 65536
LOOP = guest("(loop $forever (br $forever)) (i64.const 0)")
EMITTING = guest(f"(drop (call $emit (local.get $ptr) (local.get $len))) {ECHO_RESULT}", EMIT)
EMIT_THEN_LOOP = guest("(drop (call $emit (local.get $ptr) (local.get $len))) (loop $forever (br $forever)) "
                       "(i64.const 0)", EMIT)
FAILING = guest("(call $fail (i32.const 7)) (i64.const 0)", FAIL)
GROW = guest(f"(i32.store (i32.const 512) (memory.grow (i32.const 1000))) {WORD_RESULT}")
HUGE = guest(ECHO_RESULT, memory='(memory (export "memory") 100)')
TABLE = guest(f"(i32.store (i32.const 512) (table.grow $t (ref.null func) (i32.const 100000))) {WORD_RESULT}",
              extra="(table $t 10 funcref)")
RECURSE = guest("(call $deeper) (i64.const 0)", extra="(func $deeper (call $deeper))")
OUTSIDE = guest("(i64.or (i64.shl (i64.const 65530) (i64.const 32)) (i64.const 100))")
EMIT_OUTSIDE = guest(f"(drop (call $emit (i32.const 65530) (i32.const 100))) {ECHO_RESULT}", EMIT)


def digest(wasm: bytes) -> str:
    return hashlib.sha256(wasm).hexdigest()


def measurement_script():
    spec = util.spec_from_file_location("measure_wasm_cost", ROOT / "scripts" / "measure_wasm_cost.py")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sandbox():
    modules = [CHECKED_ADD, ECHO, DUMP, LOOP, EMITTING, EMIT_THEN_LOOP, FAILING, GROW, HUGE, TABLE, RECURSE,
               OUTSIDE, EMIT_OUTSIDE]
    with Sandbox({digest(m): f"test-{n}" for n, m in enumerate(modules)}, Limits(timeout_s=0.5)) as made:
        yield made


def test_the_release_neither_ships_nor_locks_the_webassembly_runtime():
    """001: no need to run externally written code in the release, so the runtime is not introduced there."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'packages = ["verified_execution", "verified_execution_contracts"]' in pyproject
    assert "wasmtime" not in (ROOT / "requirements-py313.lock").read_text(encoding="utf-8")
    for source in (ROOT / "src" / "verified_execution").glob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert "wasmtime" not in text and "vater_optional" not in text, source.name
    assert "外部作成コードを実行する必要性" in DOC and "必要性がないため、リリースには導入しない" in DOC


def test_the_candidate_choice_and_threat_model_name_tested_controls():
    """002: the runtime was chosen for stated reasons, and every threat names the test that covers it."""
    for heading in ("## 候補の比較と選定", "## 脅威モデル"):
        assert heading in DOC, heading
    for candidate in ("wasmtime", "wasmer", "wasm3", "Pyodide"):
        assert candidate in DOC.split("## 脅威モデル")[0], candidate
    model = DOC.split("## 脅威モデル", 1)[1].split("\n## ", 1)[0]
    named = re.findall(r"`(test_[a-z0-9_]+)`", model)
    defined = set(re.findall(r"^def (test_[a-z0-9_]+)\(", Path(__file__).read_text(encoding="utf-8"), re.M))
    assert len(named) >= 8 and set(named) <= defined, set(named) - defined


@pytest.mark.parametrize("module_name, name, grants, code", [
    ("vater", "emit", (), "import_not_granted"),
    ("vater", "unknown", ("emit",), "import_not_allowed"),
    ("host", "spawn", ("emit", "fail"), "import_not_allowed"),
])
def test_only_reviewed_and_granted_host_functions_can_be_imported(module_name, name, grants, code):
    """003: a function outside the allowlist, or one this run does not grant, can never be called."""
    wasm = guest(ECHO_RESULT, f'(import "{module_name}" "{name}" (func (param i32 i32) (result i32)))')
    with Sandbox({digest(wasm): "probe"}) as box:
        with pytest.raises(Refused) as refused:
            box.load(wasm, BYTES, grants)
        assert refused.value.code == code
        with pytest.raises(Refused) as unknown:
            box.load(wasm, BYTES, ("emit", "network"))
        assert unknown.value.code == "unknown_grant"


def test_a_reviewed_import_with_another_type_is_refused():
    wasm = guest(ECHO_RESULT, '(import "vater" "emit" (func (param i64) (result i32)))')
    with Sandbox({digest(wasm): "probe"}) as box, pytest.raises(Refused) as refused:
        box.load(wasm, BYTES, ("emit",))
    assert refused.value.code == "import_type_mismatch"


def test_no_host_secret_reaches_the_guest(sandbox, monkeypatch):
    """004: the guest sees only its input; a dump of its whole memory holds no environment or earlier input."""
    secret = b"sk-vater-test-secret-0123456789abcdef"
    monkeypatch.setenv("VATER_TEST_SECRET", secret.decode())
    echoed = sandbox.run(sandbox.load(ECHO, BYTES), {"data": b"before " + secret})
    assert echoed.status == "completed" and echoed.output == {"data": b"before " + secret}
    dumped = sandbox.run(sandbox.load(DUMP, BYTES), {"data": b"probe"})
    assert dumped.status == "completed"
    memory = dumped.output["data"]
    assert len(memory) == 65536 and secret not in memory  # each run starts from a fresh memory
    assert memory[1024:1029] == b"probe" and memory.count(0) == 65536 - 5
    for value in os.environ.values():
        if len(value) >= 8:
            assert value.encode(errors="ignore") not in memory


@pytest.mark.parametrize("name, signature", [
    ("path_open", "(param i32 i32 i32 i32 i32 i64 i64 i32 i32) (result i32)"),
    ("fd_write", "(param i32 i32 i32 i32) (result i32)"),
    ("fd_read", "(param i32 i32 i32 i32) (result i32)"),
    ("sock_accept", "(param i32 i32 i32) (result i32)"),
    ("sock_send", "(param i32 i32 i32 i32 i32) (result i32)"),
    ("environ_get", "(param i32 i32) (result i32)"),
    ("clock_time_get", "(param i32 i64 i32) (result i32)"),
    ("random_get", "(param i32 i32) (result i32)"),
])
def test_files_network_and_environment_are_not_reachable(name, signature):
    """005: no WASI is defined, so a module asking for files, sockets, clocks or the environment never starts."""
    wasm = guest(ECHO_RESULT, f'(import "wasi_snapshot_preview1" "{name}" (func {signature}))')
    with Sandbox({digest(wasm): "probe"}) as box, pytest.raises(Refused) as refused:
        box.load(wasm, BYTES, tuple(HOST_FUNCTIONS))
    assert refused.value.code == "import_not_allowed"
    assert "wasi_snapshot_preview1" in wasm_sandbox.DENIED_IMPORTS
    assert "define_wasi" not in Path(wasm_sandbox.__file__).read_text(encoding="utf-8")


def test_an_endless_loop_ends_at_the_compute_limit():
    """006: fuel bounds the instructions a guest may execute."""
    with Sandbox({digest(LOOP): "loop"}, Limits(fuel=200_000, timeout_s=30)) as box:
        started = time.monotonic()
        outcome = box.run(box.load(LOOP, BYTES), {"data": b""})
    assert outcome.status == "fuel_exhausted" and outcome.fuel_used == 200_000 and outcome.output is None
    assert time.monotonic() - started < 5


def test_large_memory_and_table_requests_are_refused(sandbox):
    """007: a module declaring more memory never starts; growing past the limit fails inside the guest."""
    huge = sandbox.run(sandbox.load(HUGE, BYTES), {"data": b"x"})
    assert huge.status == "refused" and huge.reason.startswith("resource_limit") and "memory" in huge.reason
    grown = sandbox.run(sandbox.load(GROW, WORD), {"data": b""})
    assert grown.status == "completed" and grown.output == {"value": 0xFFFF_FFFF}  # memory.grow returned -1
    table = sandbox.run(sandbox.load(TABLE, WORD), {"data": b""})
    assert table.status == "completed" and table.output == {"value": 0xFFFF_FFFF}  # table.grow returned -1


def test_a_timeout_is_interrupted_before_effects_and_unknown_after_them():
    """008: the deadline stops the guest; after an effectful host call the result is unknown, not failed."""
    limits = Limits(fuel=10**15, timeout_s=0.2)
    with Sandbox({digest(LOOP): "loop", digest(EMIT_THEN_LOOP): "emit-then-loop"}, limits) as box:
        started = time.monotonic()
        quiet = box.run(box.load(LOOP, BYTES), {"data": b""})
        delivered = []
        effectful = box.run(box.load(EMIT_THEN_LOOP, BYTES, ("emit",)), {"data": b"order-1"}, delivered.append)
    assert time.monotonic() - started < 5
    assert quiet.status == "interrupted" and quiet.effects == 0
    assert effectful.status == "unknown" and effectful.effects == 1 and delivered == [b"order-1"]
    assert "time limit" in effectful.reason and effectful.output is None


def test_fuel_exhaustion_after_an_effect_is_unknown_too():
    with Sandbox({digest(EMIT_THEN_LOOP): "emit-then-loop"}, Limits(fuel=100_000, timeout_s=30)) as box:
        outcome = box.run(box.load(EMIT_THEN_LOOP, BYTES, ("emit",)), {"data": b"x"})
    assert outcome.status == "unknown" and "compute limit" in outcome.reason


def test_every_host_function_is_reviewed_and_each_run_lists_its_grants(sandbox):
    """009: each host function has a recorded side effect and review; a run lists exactly what it was given."""
    for host in HOST_FUNCTIONS.values():
        assert host.side_effects and host.review and host.module == "vater"
        assert f"`{host.module}.{host.name}`" in DOC
    assert {key for key, host in HOST_FUNCTIONS.items() if host.effectful} == {"emit"}
    emitted = sandbox.run(sandbox.load(EMITTING, BYTES, ("emit", "fail")), {"data": b"hi"})
    assert emitted.status == "completed" and emitted.granted == ("emit",) and emitted.emitted == (b"hi",)
    plain = sandbox.run(sandbox.load(ECHO, BYTES, ("emit", "fail")), {"data": b"hi"})
    assert plain.granted == ()  # granting is not enough: only what the module imports is linked
    failed = sandbox.run(sandbox.load(FAILING, BYTES, ("fail",)), {"data": b""})
    assert failed.status == "guest_error" and "code 7" in failed.reason and failed.granted == ("fail",)


@pytest.mark.parametrize("values, problem", [
    ({"left": 1}, "exactly the declared fields"),
    ({"left": 1, "right": 2, "extra": 3}, "exactly the declared fields"),
    ({"left": -1, "right": 2}, "integer in"),
    ({"left": 2**32, "right": 2}, "integer in"),
    ({"left": True, "right": 2}, "integer in"),
    ({"left": 1.0, "right": 2}, "integer in"),
    ({"left": "1", "right": 2}, "integer in"),
], ids=["missing", "extra", "negative", "too_large", "bool", "float", "text"])
def test_input_of_the_wrong_type_or_range_is_refused_before_the_guest_runs(sandbox, values, problem):
    """010: values are checked against the declared types; nothing runs on a mismatch."""
    outcome = sandbox.run(sandbox.load(CHECKED_ADD, ADD), values)
    assert outcome.status == "refused" and problem in outcome.reason and outcome.fuel_used == 0


def test_the_interface_types_are_checked_both_ways(sandbox):
    """010: the exports' types, the output's shape and the interface itself are all checked."""
    wide = Interface(ADD.input, (Field("status", "u8"), Field("sum", "u64")))
    mismatched = sandbox.run(sandbox.load(CHECKED_ADD, wide), {"left": 1, "right": 2})
    assert mismatched.status == "guest_error" and "malformed_output" in mismatched.reason
    assert sandbox.run(sandbox.load(CHECKED_ADD, ADD), {"left": 2, "right": 3}).output == {"status": 0, "sum": 5}
    assert sandbox.run(sandbox.load(CHECKED_ADD, ADD), {"left": 2**32 - 1, "right": 1}).output == {
        "status": 1, "sum": 0}
    oversized = sandbox.run(sandbox.load(ECHO, BYTES), {"data": b"x" * (wasm_sandbox.MAX_INPUT_BYTES + 1)})
    assert oversized.status == "refused" and "larger" in oversized.reason
    for bad in (guest("(i32.const 0)", run_type="(param $ptr i32) (param $len i32) (result i32)"),
                guest(ECHO_RESULT, memory="(memory 1)")):
        with Sandbox({digest(bad): "bad"}) as box, pytest.raises(Refused) as refused:
            box.load(bad, BYTES)
        assert refused.value.code == "interface_mismatch"
    for fields in ((), (Field("a", "bytes"), Field("b", "u8")), (Field("a", "u8"), Field("a", "u8"))):
        with pytest.raises(InterfaceError):
            Interface(fields, ADD.output)
    with pytest.raises(InterfaceError):
        Field("left", "f64")


def test_an_unapproved_or_altered_module_is_never_compiled(monkeypatch):
    """011: only a module whose SHA-256 the host approved is compiled; a changed byte means not approved."""
    assert digest(CHECKED_ADD) in APPROVED  # the committed approval names exactly this binary
    compiled = []
    real_module = wasm_sandbox.Module
    monkeypatch.setattr(wasm_sandbox, "Module", lambda *args: compiled.append(1) or real_module(*args))
    with Sandbox(APPROVED) as box:
        tampered = bytearray(CHECKED_ADD)
        tampered[-1] ^= 1
        for module in (bytes(tampered), ECHO, CHECKED_ADD + b"\0"):
            with pytest.raises(Refused) as refused:
                box.load(module, ADD)
            assert refused.value.code == "unapproved_module"
        assert compiled == []
        text = (ROOT / "tests" / "fixtures" / "wasm" / "checked_add.wat").read_bytes()
        with Sandbox({digest(text): "text"}) as text_box, pytest.raises(Refused) as refused:
            text_box.load(text, ADD)  # approved by digest, but not a binary module
        assert refused.value.code == "invalid_module" and compiled == []
        assert box.run(box.load(CHECKED_ADD, ADD), {"left": 40, "right": 2}).output == {"status": 0, "sum": 42}
        assert compiled == [1]
    with pytest.raises(ValueError):
        Sandbox({"NOT-A-DIGEST": "x"})


def test_the_isolated_process_holds_no_credentials_and_cannot_write_files(tmp_path, monkeypatch):
    """012: the extra process boundary: same result, empty environment, no file writes, a CPU limit."""
    monkeypatch.setenv("VATER_TEST_SECRET", "sk-vater-test-secret-0123456789abcdef")
    isolated = run_isolated(CHECKED_ADD, ADD, {"left": 2, "right": 3}, APPROVED)
    assert isolated.status == "completed" and isolated.output == {"status": 0, "sum": 5}
    refused = run_isolated(ECHO, BYTES, {"data": b"x"}, APPROVED)
    assert refused.status == "refused" and "unapproved_module" in refused.reason
    target = tmp_path / "written.txt"
    probe = ("import json, os\nprint(json.dumps(sorted(os.environ)), flush=True)\n"
             "handle = open(sys.argv[3], 'w')\nhandle.write('x' * 10)\nhandle.flush()\nprint('wrote', flush=True)\n")
    completed = subprocess.run([sys.executable, "-I", "-c", wasm_sandbox._CHILD_LIMITS + probe, "src", "3",
                                str(target)], env={}, capture_output=True, text=True, timeout=60)
    environment = json.loads(completed.stdout.splitlines()[0])
    assert set(environment) <= {"LC_CTYPE"}  # Python's own locale coercion; nothing inherited
    # CPython ignores SIGXFSZ, so the write fails with EFBIG instead of killing the process.
    assert completed.returncode != 0 and "File too large" in completed.stderr and "wrote" not in completed.stdout
    assert target.stat().st_size == 0


@pytest.mark.parametrize("grants, status", [((), "interrupted"), (("emit",), "unknown")], ids=["quiet", "effectful"])
def test_an_isolated_process_that_dies_is_never_reported_as_finished(monkeypatch, grants, status):
    monkeypatch.setattr(wasm_sandbox.subprocess, "run",
                        lambda *args, **kwargs: subprocess.CompletedProcess(args, -9, "", ""))
    outcome = run_isolated(CHECKED_ADD, ADD, {"left": 1, "right": 2}, APPROVED, grants)
    assert outcome.status == status and outcome.output is None


def test_resource_exhaustion_and_escape_attempts_stay_inside(sandbox):
    """013: recursion, out-of-bounds output and emits, and huge declarations end without any effect outside."""
    recursion = sandbox.run(sandbox.load(RECURSE, BYTES), {"data": b""})
    assert recursion.status == "guest_error" and "STACK_OVERFLOW" in recursion.reason
    outside = sandbox.run(sandbox.load(OUTSIDE, BYTES), {"data": b""})
    assert outside.status == "guest_error" and "outside" in outside.reason
    delivered = []
    escaped = sandbox.run(sandbox.load(EMIT_OUTSIDE, BYTES, ("emit",)), {"data": b""}, delivered.append)
    assert escaped.status == "guest_error" and escaped.effects == 0 and delivered == []
    for outcome in (recursion, outside, escaped):
        assert outcome.output is None and outcome.status != "completed"
    for _ in range(50):  # repeated runs start from fresh stores; nothing accumulates
        assert sandbox.run(sandbox.load(GROW, WORD), {"data": b""}).output == {"value": 0xFFFF_FFFF}


def test_a_failing_sink_makes_the_result_unknown(sandbox):
    def broken(chunk: bytes) -> None:
        raise OSError("the downstream system failed mid-write")

    outcome = sandbox.run(sandbox.load(EMITTING, BYTES, ("emit",)), {"data": b"x"}, broken)
    assert outcome.status == "unknown" and "host_error" in outcome.reason


def test_the_cost_comparison_is_measured_and_recorded():
    """014: Python core against WebAssembly isolation, in security and cost, from measured values."""
    record = json.loads((ROOT / "evidence" / "optional" / "webassembly-cost.json").read_text(encoding="utf-8"))
    assert record["wasmtime"] == "50.0.1" and record["iterations"] >= 1000
    timing = record["per_call_microseconds"]
    assert 0 < timing["python_core"] < timing["wasm_in_process"] < timing["wasm_isolated_process"]
    assert record["compile_milliseconds"] > 0 and record["added_installed_bytes"] > 1_000_000
    assert record["security"]["host_functions_exposed"] == len(HOST_FUNCTIONS)
    section = DOC.split("## 運用負担の比較", 1)[1]
    for key in ("python_core", "wasm_in_process", "wasm_isolated_process"):
        assert f"{timing[key]:.1f}" in section, key
    fresh = measurement_script().measure(iterations=50, isolated_iterations=2)
    assert fresh["per_call_microseconds"]["python_core"] < fresh["per_call_microseconds"]["wasm_isolated_process"]
