"""task.formal_verification.013: no Kani assumption is unjustified or removes the verified cases.

docs/kani_assumptions.json lists every harness of rust/vater_core/src/proofs.rs with each
kani::assume and its justification and the covers Kani must satisfy. These tests keep the
audit identical to the source (no unlisted or unjustified assumption, no vacuous
`assume(false)`, no stubs or expected panics, a cover for each harness), and check that
scripts/check_kani.py fails a Kani run with a failed harness, an unsatisfied cover, a
missing or extra harness, or no summary.
"""
from __future__ import annotations

from importlib import util
import json
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROOFS = (ROOT / "rust" / "vater_core" / "src" / "proofs.rs").read_text(encoding="utf-8")
LIBRARY = (ROOT / "rust" / "vater_core" / "src" / "lib.rs").read_text(encoding="utf-8")
AUDIT = json.loads((ROOT / "docs" / "kani_assumptions.json").read_text(encoding="utf-8"))
spec = util.spec_from_file_location("check_kani", ROOT / "scripts" / "check_kani.py")
check_kani = util.module_from_spec(spec)
spec.loader.exec_module(check_kani)


def harnesses(source: str) -> dict[str, dict]:
    found = {}
    pieces = re.split(r"#\[kani::proof\]", source)[1:]
    for piece in pieces:
        name = re.search(r"fn (\w+)\(", piece).group(1)
        body = piece.split(f"fn {name}(", 1)[1]
        unwind = re.search(r"#\[kani::unwind\((\d+)\)\]", piece.split(f"fn {name}(", 1)[0])
        found[name] = {
            "assumptions": re.findall(r"kani::assume\((.*?)\);", body),
            "covers": re.findall(r'kani::cover!\(\s*.*?,\s*"([^"]+)"\s*\)', body, flags=re.S),
            "unwind": int(unwind.group(1)) if unwind else None,
        }
    return found


SOURCE = harnesses(PROOFS)


def test_every_harness_is_audited_and_every_audited_harness_exists():
    assert set(SOURCE) == set(AUDIT["harnesses"]) and len(SOURCE) == 6


@pytest.mark.parametrize("name", sorted(AUDIT["harnesses"]))
def test_each_assumption_is_listed_with_a_justification(name):
    audited = AUDIT["harnesses"][name]
    assert [a["expression"] for a in audited["assumptions"]] == SOURCE[name]["assumptions"]
    for assumption in audited["assumptions"]:
        assert len(assumption["justification"]) >= 80, assumption
        assert assumption["expression"].strip() not in {"false", "0 == 1"}
    assert audited["unwind"] == SOURCE[name]["unwind"]
    assert audited["task"] in {"task.formal_verification.011", "task.formal_verification.012"}
    assert audited["input_domain"] and audited["excluded"]


@pytest.mark.parametrize("name", sorted(AUDIT["harnesses"]))
def test_each_harness_has_the_audited_covers(name):
    assert SOURCE[name]["covers"] == AUDIT["harnesses"][name]["covers"]
    assert SOURCE[name]["covers"], "a harness without a cover could be vacuous unnoticed"


def test_nothing_replaces_or_weakens_the_verified_code():
    for forbidden in ("kani::stub", "should_panic", "kani::assume(false)", "unsafe "):
        assert forbidden not in PROOFS, forbidden
    assert "#![forbid(unsafe_code)]" in LIBRARY
    assert "#[cfg(kani)]\nmod proofs;" in LIBRARY  # the harnesses are compiled only for Kani


def kani_output(results: dict[str, tuple[str, int, int]], failures: int = 0) -> str:
    lines = []
    for name, (verdict, satisfied, total) in results.items():
        lines += [f"Checking harness proofs::{name}...", "SUMMARY:", " ** 0 of 9 failed", "",
                  f" ** {satisfied} of {total} cover properties satisfied", "", f"VERIFICATION:- {verdict}"]
    verified = sum(1 for verdict, _, _ in results.values() if verdict == "SUCCESSFUL")
    lines.append(f"Complete - {verified} successfully verified harnesses, {failures} failures, {len(results)} total.")
    return "\n".join(lines)


def all_passing():
    return {name: ("SUCCESSFUL", len(entry["covers"]), len(entry["covers"]))
            for name, entry in AUDIT["harnesses"].items()}


def test_the_checker_accepts_a_run_where_everything_verified():
    parsed = check_kani.parse(kani_output(all_passing()))
    assert check_kani.problems(parsed, AUDIT) == []


@pytest.mark.parametrize("defect", ["failed", "unsatisfied_cover", "missing", "extra", "no_summary"])
def test_the_checker_refuses_every_kind_of_incomplete_run(defect):
    results = all_passing()
    name = "permits_is_the_grant_policy_for_every_grant_and_request"
    text = None
    if defect == "failed":
        results[name] = ("FAILED", 3, 3)
        text = kani_output(results, failures=1)
    elif defect == "unsatisfied_cover":
        results[name] = ("SUCCESSFUL", 2, 3)
    elif defect == "missing":
        del results[name]
    elif defect == "extra":
        results["an_unaudited_harness"] = ("SUCCESSFUL", 1, 1)
    text = text or kani_output(results)
    if defect == "no_summary":
        text = text.rsplit("\nComplete", 1)[0]
    assert check_kani.problems(check_kani.parse(text), AUDIT) != []


def test_the_formal_job_runs_kani_and_the_checker():
    workflow = (ROOT / ".github/workflows/python-verification.yml").read_text(encoding="utf-8")
    job = re.split(r"\n  (?=[A-Za-z0-9_-]+:\n)", workflow.split("  formal-rust:\n", 1)[1], maxsplit=1)[0]
    for step in ("cargo test --locked", f"cargo install --locked kani-verifier --version {AUDIT['kani_version']}",
                 "cargo kani setup", "cargo kani 2>&1 | tee ../../kani-output.txt",
                 "python scripts/check_kani.py kani-output.txt --record evidence/formal/kani-results.json"):
        assert step in job, step
    assert "shell: bash" in job  # pipefail: a failing cargo kani fails the step despite tee


def test_the_recorded_kani_run_verified_every_audited_harness_with_all_covers():
    record = json.loads((ROOT / "evidence" / "formal" / "kani-results.json").read_text(encoding="utf-8"))
    assert check_kani.problems({"harnesses": record["harnesses"], "summary": record["summary"]}, AUDIT) == []
    assert record["tools"]["kani"] == AUDIT["kani_version"]
    covers = sum(len(entry["covers"]) for entry in AUDIT["harnesses"].values())
    assert record["check_statuses"]["SATISFIED"] == covers  # every cover reached: no vacuous harness
    assert set(record["check_statuses"]) <= {"SUCCESS", "SATISFIED", "UNREACHABLE"}  # nothing failed or undetermined
