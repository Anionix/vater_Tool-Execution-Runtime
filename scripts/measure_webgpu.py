#!/usr/bin/env python3
"""task.optional_webgpu.003 / 009 / 012: measure the CPU baselines and the WebGPU path, then decide.

  python scripts/measure_webgpu.py --output evidence/optional/webgpu-measurements.json

For each size the record keeps the Python reference's time and peak memory (tracemalloc),
the same multiplication as a plain JavaScript loop in the page, the shader pipeline's
creation time, and the WebGPU round trip including buffer creation, upload, dispatch and
readback (median of the repeats). The GPU result is checked against the reference with the
tolerance of matmul.tolerance(). The adoption rule (docs/optional/WEBGPU.md) is applied to
the numbers: WebGPU stays only if, on a hardware adapter, its round trip is at least twice as
fast as the fastest CPU path at every measured size, with every result within tolerance.
Needs requirements-optional-py313.lock (Playwright 1.63.0); the adapter here is SwiftShader.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from playwright.sync_api import sync_playwright  # noqa: E402

from vater_optional import matmul  # noqa: E402
from vater_optional.webmcp_host import Host, create_app, serve  # noqa: E402

GPU_ARGS = ["--enable-unsafe-webgpu", "--use-angle=swiftshader", "--use-webgpu-adapter=swiftshader",
            "--enable-features=Vulkan"]
REQUIRED_SPEEDUP = 2.0


def python_reference(n: int, a: list[float], b: list[float]) -> tuple[float, int, list[float]]:
    tracemalloc.start()
    started = time.perf_counter()
    exact = matmul.reference(n, a, b)
    seconds = time.perf_counter() - started
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return seconds, peak, exact


def decide(sizes: list[dict], adapter: dict) -> dict:
    hardware = not adapter["fallback"]
    fast_enough = all(size["gpu_total_ms_median"] * REQUIRED_SPEEDUP <= size["fastest_cpu_ms"] for size in sizes)
    accurate = all(size["within_tolerance"] for size in sizes)
    return {"rule": f"hardware adapter, round trip >= {REQUIRED_SPEEDUP}x faster than the fastest CPU path at "
                    "every size, every result within tolerance",
            "hardware_adapter": hardware, "fast_enough": fast_enough, "accurate": accurate,
            "adopt": hardware and fast_enough and accurate}


def measure(sizes=(32, 64, 128), repeats: int = 5, playwright=None) -> dict:
    rows = []
    with serve(create_app(Host("measurement-operator-0123456789abcdef"), directory="webgpu")) as port:
        def run(running):
            browser = running.chromium.launch(args=GPU_ARGS)
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{port}/")
                page.wait_for_selector("body[data-ready=true]")
                adapter = page.evaluate("async () => (await import('/matmul.js')).adapterInfo()")
                for n in sizes:
                    a, b = matmul.generate(n, seed=n)
                    seconds, peak, _ = python_reference(n, a, b)
                    found = page.evaluate("""async ([n, a, b, repeats]) => (await import('/matmul.js'))
                        .measure(n, new Float32Array(a), new Float32Array(b), repeats)""", [n, a, b, repeats])
                    checked = matmul.compare(n, a, b, found["result"])
                    gpu = statistics.median(found["gpuMs"])
                    cpu_js = statistics.median(found["cpuJsMs"])
                    rows.append({"n": n, "python_reference_ms": round(seconds * 1000, 1),
                                 "python_reference_peak_bytes": peak,
                                 "js_cpu_ms_median": round(cpu_js, 2),
                                 "gpu_pipeline_ms": round(found["pipelineMs"], 2),
                                 "gpu_total_ms_median": round(gpu, 2),
                                 "fastest_cpu_ms": round(min(cpu_js, seconds * 1000), 2),
                                 "within_tolerance": checked["within_tolerance"],
                                 "max_error_to_tolerance": checked["max_error_to_tolerance"]})
                return adapter, browser.version
            finally:
                browser.close()

        if playwright is not None:
            adapter, version = run(playwright)
        else:
            with sync_playwright() as running:
                adapter, version = run(running)
    return {"browser": version, "adapter": adapter, "repeats": repeats, "sizes": rows,
            "decision": decide(rows, adapter)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    record = measure()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    for row in record["sizes"]:
        print(row)
    print(record["decision"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
