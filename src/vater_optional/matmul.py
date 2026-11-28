"""Matrix multiplication: the WebGPU prototype's CPU reference and host checks (task.optional_webgpu.*).

001  The parallel computation chosen: C = A x B for square row-major float32 matrices,
     1 <= n <= 512.
002  reference() is the CPU reference with fixed input, output and precision: inputs are
     float32 values, each element of C is the exactly rounded float32 of the exact sum
     (every product is summed with math.fsum in float64, then rounded once to float32).
     generate() makes deterministic inputs that are multiples of 1/1024 in [-1, 1], for
     which every product and every sum is exact in float64, so the reference is exact.
004  A matrix may go to the GPU only if its data classification is public or synthetic;
     anything else (and any unknown label) is refused before it leaves the host.
006  validate() refuses a size outside [1, 512], a length other than n*n, and non-finite
     values, before any buffer is made.
007  tolerance() is the allowed error of an f32 accumulation in any order:
       |gpu - exact| <= n * 2**-24 * sum_k |a_ik * b_kj| + 2**-24 * |exact|
     (the standard bound gamma_n for n float32 additions, plus the final rounding).
011  accept_gpu_result() treats what the GPU returns as untrusted input: wrong length,
     non-finite values or any element outside the tolerance is refused by the host.
"""
from __future__ import annotations

from array import array
import math
import random
import struct

MAX_N = 512
UNIT = 2.0 ** -24  # float32 unit roundoff
GPU_ALLOWED = frozenset({"public", "synthetic"})
KNOWN_CLASSIFICATIONS = frozenset({"public", "synthetic", "internal", "confidential", "personal"})


def f32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def generate(n: int, seed: int) -> tuple[list[float], list[float]]:
    """Deterministic inputs: multiples of 1/1024 in [-1, 1], exactly representable in float32."""
    rng = random.Random(seed)
    return ([rng.randint(-1024, 1024) / 1024 for _ in range(n * n)],
            [rng.randint(-1024, 1024) / 1024 for _ in range(n * n)])


def validate(n: object, a: object, b: object) -> None:
    if type(n) is not int or not 1 <= n <= MAX_N:
        raise ValueError("invalid_size")
    for matrix in (a, b):
        if not isinstance(matrix, (list, tuple, array)) or len(matrix) != n * n:
            raise ValueError("invalid_length")
        if not all(isinstance(value, float) and math.isfinite(value) and f32(value) == value for value in matrix):
            raise ValueError("not_float32")


def reference(n: int, a: list[float], b: list[float]) -> list[float]:
    validate(n, a, b)
    columns = [b[k * n:(k + 1) * n] for k in range(n)]
    out = []
    for i in range(n):
        row = a[i * n:(i + 1) * n]
        for j in range(n):
            out.append(f32(math.fsum(row[k] * columns[k][j] for k in range(n))))
    return out


def tolerance(n: int, a: list[float], b: list[float], exact: list[float]) -> list[float]:
    bounds = []
    for i in range(n):
        row = a[i * n:(i + 1) * n]
        for j in range(n):
            magnitude = math.fsum(abs(row[k] * b[k * n + j]) for k in range(n))
            bounds.append(n * UNIT * magnitude + UNIT * abs(exact[i * n + j]))
    return bounds


def compare(n: int, a: list[float], b: list[float], result: list[float]) -> dict:
    exact = reference(n, a, b)
    bounds = tolerance(n, a, b, exact)
    errors = [abs(r - e) for r, e in zip(result, exact)]
    ratios = [error / bound if bound else (0.0 if error == 0 else math.inf) for error, bound in zip(errors, bounds)]
    violations = [index for index, ratio in enumerate(ratios) if ratio > 1.0]
    return {"max_abs_error": max(errors), "max_error_to_tolerance": max(ratios), "violations": len(violations),
            "within_tolerance": not violations and len(result) == n * n}


def gpu_destination_allowed(classification: str) -> bool:
    if classification not in KNOWN_CLASSIFICATIONS:
        raise ValueError(f"unknown data classification: {classification!r}")
    return classification in GPU_ALLOWED


def accept_gpu_result(n: int, a: list[float], b: list[float], result: object) -> list[float]:
    if not isinstance(result, (list, tuple)) or len(result) != n * n:
        raise ValueError("gpu_result_length")
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in result):
        raise ValueError("gpu_result_not_finite")
    checked = compare(n, a, b, [float(value) for value in result])
    if not checked["within_tolerance"]:
        raise ValueError(f"gpu_result_outside_tolerance: {checked['violations']} element(s)")
    return [float(value) for value in result]
