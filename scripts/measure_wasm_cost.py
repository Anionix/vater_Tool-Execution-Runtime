#!/usr/bin/env python3
"""task.optional_webassembly.014: measure the Python core against WebAssembly isolation.

  python scripts/measure_wasm_cost.py --output evidence/optional/webassembly-cost.json

The same addition runs three ways: core.checked_add in the host process, the approved
checked_add guest in a fresh wasmtime store per call, and that guest behind run_isolated's
process boundary. The record keeps the median per-call time of each (five batches), the
compile time, the installed size the wasmtime wheel adds, and what each path exposes, so
docs/optional/WEBASSEMBLY.md compares security and cost with numbers, not adjectives.
Needs requirements-optional-py313.lock.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib.metadata import distribution, version
import json
from pathlib import Path
import platform
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wasmtime import wat2wasm  # noqa: E402

from vater_optional import wasm_sandbox  # noqa: E402
from vater_optional.wasm_sandbox import Field, Interface, Sandbox, run_isolated  # noqa: E402
from verified_execution.core import checked_add  # noqa: E402

ADD = Interface((Field("left", "u32"), Field("right", "u32")), (Field("status", "u8"), Field("sum", "u32")))


def per_call_microseconds(call, iterations: int, batches: int = 5) -> float:
    results = []
    for _ in range(batches):
        started = time.perf_counter_ns()
        for _ in range(iterations):
            call()
        results.append((time.perf_counter_ns() - started) / iterations / 1000)
    return round(statistics.median(results), 3)


def installed_bytes(name: str) -> int:
    found = distribution(name)
    return sum(path.size or 0 for path in found.files or [])


def measure(iterations: int = 2000, isolated_iterations: int = 20) -> dict:
    wasm = wat2wasm((ROOT / "tests" / "fixtures" / "wasm" / "checked_add.wat").read_text(encoding="utf-8"))
    approved = {hashlib.sha256(wasm).hexdigest(): "checked_add"}
    with Sandbox(approved) as sandbox:
        started = time.perf_counter_ns()
        loaded = sandbox.load(wasm, ADD)
        compile_ms = (time.perf_counter_ns() - started) / 1e6
        assert sandbox.run(loaded, {"left": 2, "right": 3}).output == {"status": 0, "sum": 5}
        in_process = per_call_microseconds(lambda: sandbox.run(loaded, {"left": 2, "right": 3}), iterations)
    assert checked_add(2, 3) == 5
    core = per_call_microseconds(lambda: checked_add(2, 3), iterations)
    isolated = per_call_microseconds(lambda: run_isolated(wasm, ADD, {"left": 2, "right": 3}, approved),
                                     isolated_iterations, batches=3)
    return {
        "measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "python": platform.python_version(), "machine": platform.machine(), "wasmtime": version("wasmtime"),
        "iterations": iterations, "isolated_iterations": isolated_iterations,
        "per_call_microseconds": {"python_core": core, "wasm_in_process": in_process,
                                  "wasm_isolated_process": isolated},
        "compile_milliseconds": round(compile_ms, 2),
        "added_installed_bytes": installed_bytes("wasmtime"),
        "added_locked_packages": ["wasmtime"],
        "prototype_source_lines": len(Path(wasm_sandbox.__file__).read_text(encoding="utf-8").splitlines()),
        "security": {
            "host_functions_exposed": len(wasm_sandbox.HOST_FUNCTIONS),
            "effectful_host_functions": sum(1 for host in wasm_sandbox.HOST_FUNCTIONS.values() if host.effectful),
            "wasi_capabilities_linked": 0,
            "checks_before_the_guest_runs": ["approved SHA-256", "binary core module", "export types",
                                             "imports reviewed and granted", "input types and ranges"],
            "python_core_runs_external_code": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=2000)
    args = parser.parse_args()
    record = measure(args.iterations)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(record["per_call_microseconds"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
