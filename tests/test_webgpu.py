"""task.optional_webgpu.001-012: the WebGPU matrix multiplication prototype.

Runs only with requirements-optional-py313.lock (Playwright 1.63.0 and its Chromium build);
the base jobs skip this module. There is no GPU here or in CI, so the shader runs on
Chromium's SwiftShader adapter (a software GPU): correctness, validation, device loss and the
host's acceptance check are executed for real, while the speed numbers describe SwiftShader.
"""
from __future__ import annotations

from fractions import Fraction
from importlib import util
import json
from pathlib import Path
import random
import re

import pytest

pytest.importorskip("playwright")
pytest.importorskip("uvicorn")

from playwright.sync_api import sync_playwright  # noqa: E402

from vater_optional import matmul  # noqa: E402
from vater_optional.webmcp_host import Host, create_app, serve  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "optional" / "WEBGPU.md").read_text(encoding="utf-8")
SHADER = (ROOT / "web" / "webgpu" / "matmul.wgsl").read_text(encoding="utf-8")
RECORD = json.loads((ROOT / "evidence" / "optional" / "webgpu-measurements.json").read_text(encoding="utf-8"))
GPU_ARGS = ["--enable-unsafe-webgpu", "--use-angle=swiftshader", "--use-webgpu-adapter=swiftshader",
            "--enable-features=Vulkan"]
COMPUTE = """async ([n, a, b]) => (await import('/matmul.js'))
    .computeWithRetry(n, new Float32Array(a), new Float32Array(b))"""


def section(task: str) -> str:
    return DOC.split(f"（task.optional_webgpu.{task}）", 1)[1].split("\n## ", 1)[0]


def general_inputs(n: int, seed: int) -> tuple[list[float], list[float]]:
    """float32 values that are not short binary fractions, so float32 sums do round."""
    rng = random.Random(seed)
    return ([matmul.f32(rng.uniform(-1, 1)) for _ in range(n * n)],
            [matmul.f32(rng.uniform(-1, 1)) for _ in range(n * n)])


def measurement_script():
    spec = util.spec_from_file_location("measure_webgpu", ROOT / "scripts" / "measure_webgpu.py")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def playwright():
    with sync_playwright() as running:
        yield running


@pytest.fixture(scope="module")
def page(playwright):
    with serve(create_app(Host("webgpu-test-operator-0123456789abcdef"), directory="webgpu")) as port:
        browser = playwright.chromium.launch(args=GPU_ARGS)
        opened = browser.new_page()
        opened.goto(f"http://127.0.0.1:{port}/")
        opened.wait_for_selector("body[data-ready=true]")
        yield opened
        browser.close()


def test_the_chosen_parallel_computation_is_matrix_multiplication():
    """001: C = A x B for square float32 matrices; checked on a 2 x 2 case by hand."""
    assert matmul.reference(2, [1.0, 2.0, 3.0, 4.0], [5.0, 6.0, 7.0, 8.0]) == [19.0, 22.0, 43.0, 50.0]
    assert matmul.MAX_N == 512 and "行列の積" in section("001")


def test_the_cpu_reference_is_the_exactly_rounded_sum():
    """002: fixed inputs, float32 output, each element the exact sum rounded once."""
    for n, (a, b) in ((5, matmul.generate(5, 7)), (4, general_inputs(4, 3))):
        exact = [Fraction(0)] * (n * n)
        for i in range(n):
            for j in range(n):
                exact[i * n + j] = sum(Fraction(a[i * n + k]) * Fraction(b[k * n + j]) for k in range(n))
        assert matmul.reference(n, a, b) == [matmul.f32(float(value)) for value in exact]
    assert matmul.generate(8, 1) == matmul.generate(8, 1) and matmul.generate(8, 1) != matmul.generate(8, 2)
    a, _ = matmul.generate(8, 1)
    assert all(matmul.f32(value) == value and abs(value) <= 1 for value in a)


def test_the_baseline_speed_and_memory_are_recorded():
    """003: the Python reference's time and peak memory are kept for each size."""
    rows = RECORD["sizes"]
    assert [row["n"] for row in rows] == [32, 64, 128]
    assert all(row["python_reference_ms"] > 0 and row["python_reference_peak_bytes"] > 0 for row in rows)
    assert rows[0]["python_reference_peak_bytes"] < rows[-1]["python_reference_peak_bytes"]
    text = section("003")
    for row in rows:
        assert f"{row['python_reference_ms']} ミリ秒" in text and f"{row['python_reference_peak_bytes']:,} バイト" in text


@pytest.mark.parametrize("classification, allowed", [
    ("public", True), ("synthetic", True), ("internal", False), ("confidential", False), ("personal", False)])
def test_only_public_or_synthetic_data_may_go_to_the_gpu(classification, allowed):
    """004: the classification decides whether a matrix may leave the host for the GPU."""
    assert matmul.gpu_destination_allowed(classification) is allowed
    row = [line for line in section("004").splitlines() if line.startswith(f"| {classification} |")]
    assert row and ("| 可 |" in row[0]) is allowed


def test_an_unknown_classification_is_refused():
    with pytest.raises(ValueError):
        matmul.gpu_destination_allowed("secret-but-unlabelled")


def test_the_shader_declares_its_input_and_output_types(page):
    """005: two read-only f32 inputs, one f32 output, a u32 size; and it compiles and runs."""
    bindings = dict(re.findall(r"@binding\((\d)\) var<([a-z_, ]+)> \w+: [A-Za-z0-9<>]+;", SHADER))
    assert bindings == {"0": "storage, read", "1": "storage, read", "2": "storage, read_write", "3": "uniform"}
    assert re.findall(r"var<storage, [a-z_]+> (\w+): array<f32>;", SHADER) == ["a", "b", "c"]
    assert "struct Dims {\n  n: u32,\n}" in SHADER and "@workgroup_size(8, 8)" in SHADER
    a, b = matmul.generate(8, 5)
    assert page.evaluate(COMPUTE, [8, a, b])["status"] == "gpu"


@pytest.mark.parametrize("n, length, value, problem", [
    (0, 0, 0.5, "invalid_size"), (513, 4, 0.5, "invalid_size"), (2.5, 4, 0.5, "invalid_size"),
    (3, 8, 0.5, "invalid_length"), (3, 10, 0.5, "invalid_length"), (2, 4, "NaN", "not_finite"),
    (2, 4, "Infinity", "not_finite"),
], ids=["zero", "too_large", "fraction", "short", "long", "nan", "infinite"])
def test_invalid_sizes_and_lengths_are_refused_before_any_buffer(page, n, length, value, problem):
    """006: the page refuses before creating a buffer; the host refuses the same inputs."""
    outcome = page.evaluate("""async ([n, length, value]) => {
        const fill = () => new Float32Array(length).fill(value === "NaN" ? NaN : value === "Infinity" ? Infinity : value);
        return (await import('/matmul.js')).computeWithRetry(n, fill(), fill());
    }""", [n, length, value])
    assert outcome == {"status": "refused", "reason": problem, "attempts": 0, "losses": []}
    plain = page.evaluate("async () => (await import('/matmul.js')).validateInput(2, [1, 2, 3, 4], new Float32Array(4))")
    assert plain == "not_float32"
    host_value = {"NaN": float("nan"), "Infinity": float("inf")}.get(value, value)
    with pytest.raises(ValueError):
        matmul.validate(n, [host_value] * length, [host_value] * length)


def test_sizes_that_are_not_multiples_of_the_workgroup_stay_in_bounds(page):
    """006: invocations outside the matrix return before touching memory."""
    assert "if (id.x >= n || id.y >= n) {\n    return;" in SHADER
    for n in (1, 7, 13):
        a, b = matmul.generate(n, n)
        outcome = page.evaluate(COMPUTE, [n, a, b])
        assert outcome["status"] == "gpu" and outcome["result"] == matmul.reference(n, a, b)


def test_the_error_tolerance_is_specified_and_catches_wrong_values(page):
    """007: the tolerance is the float32 accumulation bound; a value just outside it is caught."""
    assert "n × 2^-24 × Σ_k |a_ik × b_kj| + 2^-24 × |正確な値|" in section("007")
    n = 24
    a, b = general_inputs(n, 11)
    cpu = page.evaluate("async ([n, a, b]) => Array.from((await import('/matmul.js')).cpuMatmul(n, "
                        "new Float32Array(a), new Float32Array(b)))", [n, a, b])
    checked = matmul.compare(n, a, b, cpu)
    assert checked["within_tolerance"] and checked["max_abs_error"] > 0  # sequential float32 sums do round
    exact = matmul.reference(n, a, b)
    bounds = matmul.tolerance(n, a, b, exact)
    wrong = list(cpu)
    wrong[5] = exact[5] + 2 * bounds[5]
    assert not matmul.compare(n, a, b, wrong)["within_tolerance"]


@pytest.mark.parametrize("n, inputs", [(16, "exact"), (64, "exact"), (13, "general"), (48, "general")])
def test_cpu_and_gpu_results_agree_within_tolerance(page, n, inputs):
    """008: the GPU's result matches the CPU reference within the specified tolerance."""
    a, b = matmul.generate(n, n) if inputs == "exact" else general_inputs(n, n)
    outcome = page.evaluate(COMPUTE, [n, a, b])
    assert outcome["status"] == "gpu"
    checked = matmul.compare(n, a, b, outcome["result"])
    assert checked["within_tolerance"], checked
    assert matmul.accept_gpu_result(n, a, b, outcome["result"]) == outcome["result"]


def test_total_time_including_transfer_is_measured(playwright):
    """009: the GPU round trip includes buffer creation, upload and readback, next to the CPU paths."""
    for row in RECORD["sizes"]:
        assert row["gpu_total_ms_median"] > 0 and row["js_cpu_ms_median"] >= 0 and row["within_tolerance"]
        cells = [cell.strip() for cell in next(line for line in section("009").splitlines()
                                               if line.startswith(f"| {row['n']} |")).split("|")[1:-1]]
        assert cells == [str(row["n"]), f"{row['gpu_total_ms_median']} ミリ秒", f"{row['js_cpu_ms_median']} ミリ秒",
                         f"{row['python_reference_ms']} ミリ秒"]
    assert RECORD["adapter"] == {"vendor": "google", "architecture": "swiftshader", "fallback": True}
    fresh = measurement_script().measure(sizes=(16,), repeats=2, playwright=playwright)
    assert fresh["sizes"][0]["gpu_total_ms_median"] > 0 and fresh["sizes"][0]["within_tolerance"]


def test_a_lost_device_is_retried_once_and_never_endlessly(page):
    """010: at most one new device after a loss, then the CPU; a single loss recovers on the GPU."""
    a, b = matmul.generate(9, 2)
    script = """async ([n, a, b, loseEvery]) => {
        const m = await import('/matmul.js');
        let calls = 0;
        const getDevice = async () => {
            calls += 1;
            const device = await m.requestDevice();
            if (loseEvery || calls === 1) device.destroy();
            return device;
        };
        const outcome = await m.computeWithRetry(n, new Float32Array(a), new Float32Array(b), {getDevice});
        return {...outcome, calls};
    }"""
    always = page.evaluate(script, [9, a, b, True])
    assert always["status"] == "cpu_fallback" and always["calls"] == 2 and len(always["losses"]) == 2
    assert always["result"] == matmul.reference(9, a, b)
    once = page.evaluate(script, [9, a, b, False])
    assert once["status"] == "gpu" and once["calls"] == 2 and len(once["losses"]) == 1
    assert once["result"] == matmul.reference(9, a, b)
    assert "MAX_DEVICE_RETRIES = 1" in (ROOT / "web" / "webgpu" / "matmul.js").read_text(encoding="utf-8")


@pytest.mark.parametrize("damage", ["short", "nan", "off_by_a_lot", "not_a_list"])
def test_the_gpu_is_not_a_security_boundary_and_its_result_is_checked(damage):
    """011: the host decides and checks; what the GPU returns is untrusted input."""
    n = 6
    a, b = matmul.generate(n, 4)
    result = matmul.reference(n, a, b)
    damaged = {"short": result[:-1], "nan": [float("nan")] + result[1:],
               "off_by_a_lot": [result[0] + 1.0] + result[1:], "not_a_list": "{}"}[damage]
    with pytest.raises(ValueError):
        matmul.accept_gpu_result(n, a, b, damaged)
    text = section("011")
    assert "隔離と認可の責任は Python ホストの共通中核にあり" in text and "`accept_gpu_result()`" in text


def test_adoption_is_decided_by_the_measurements():
    """012: the rule is applied to the recorded numbers, and the decision follows from them."""
    decide = measurement_script().decide
    assert decide(RECORD["sizes"], RECORD["adapter"]) == RECORD["decision"]
    assert RECORD["decision"]["adopt"] is False and RECORD["decision"]["hardware_adapter"] is False
    hardware = [{**row, "gpu_total_ms_median": row["fastest_cpu_ms"] / 4} for row in RECORD["sizes"]]
    assert decide(hardware, {**RECORD["adapter"], "fallback": False})["adopt"] is True  # the rule can say yes
    text = section("012")
    assert "2 倍以上速い" in text and "規則により採用しない" in text
