"""task.operations.001: Python test environment and formal-proof toolchains are separate.

A production candidate runs on the hash-locked Python dependency set, and a
Lean or Kani toolchain that is not installed (or not at the pinned version)
is never treated as a success. The Python workflows install no proof tools;
CI checks only the recorded Lean evidence by its hashes.
"""
from __future__ import annotations

from importlib import util
from pathlib import Path
import stat

ROOT = Path(__file__).resolve().parents[1]
spec = util.spec_from_file_location("check_toolchains", ROOT / "scripts" / "check_toolchains.py")
toolchains = util.module_from_spec(spec)
spec.loader.exec_module(toolchains)


def fake_tool(directory: Path, name: str, output: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/sh\necho '{output}'\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_missing_formal_tools_are_reported_and_never_passed(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))  # an empty directory: no lean, no kani
    result = toolchains.report()
    assert result["formal"] == {"lean": {"state": "not_installed"}, "kani": {"state": "not_installed"}}
    assert result["formal_proofs_run_here"] is False
    assert "pass" not in str(result["formal"])


def test_a_lean_at_another_version_is_a_mismatch(monkeypatch, tmp_path):
    fake_tool(tmp_path, "lean", "Lean (version 4.18.0, x86_64-unknown-linux-gnu)")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert toolchains.report()["formal"]["lean"] == {"state": "version_mismatch", "version": "4.18.0",
                                                     "expected": "4.19.0"}


def test_the_pinned_lean_is_only_available_not_a_proof(monkeypatch, tmp_path):
    fake_tool(tmp_path, "lean", "Lean (version 4.19.0, x86_64-unknown-linux-gnu)")
    monkeypatch.setenv("PATH", str(tmp_path))
    result = toolchains.report()
    assert result["formal"]["lean"] == {"state": "available", "version": "4.19.0"}
    assert result["formal_proofs_run_here"] is False


def test_an_unlocked_python_environment_fails_the_candidate_check(monkeypatch, capsys):
    locked = toolchains.locked_versions()
    assert locked["mcp"] == "2.3.0" and len(locked) >= 30
    monkeypatch.setattr(toolchains, "installed_version", lambda name: {**locked, "pydantic": "0.0.1"}.get(name))
    assert toolchains.main(["--require-locked-python"]) == 1
    report = capsys.readouterr().out
    assert '"name": "pydantic"' in report and '"installed": "0.0.1"' in report
    assert toolchains.main([]) == 0  # without the flag it only reports


def test_a_locked_environment_passes(monkeypatch):
    locked = toolchains.locked_versions()
    monkeypatch.setattr(toolchains, "installed_version", locked.get)
    assert toolchains.python_environment() == {"locked": True, "mismatches": []}
    assert toolchains.main(["--require-locked-python"]) == 0
    missing = toolchains.python_environment(lambda name: None)
    assert missing["locked"] is False and len(missing["mismatches"]) == len(locked)


def test_python_workflows_install_no_proof_tools_and_the_locked_job_checks_the_environment():
    workflows = {path.name: path.read_text() for path in (ROOT / ".github" / "workflows").glob("*.yml")}
    for name, text in workflows.items():
        for proof_tool in ("elan", "leanprover", "cargo-kani", "kani-verifier"):
            assert proof_tool not in text, (name, proof_tool)
    verification = workflows["python-verification.yml"]
    locked_step = verification.index("python scripts/check_toolchains.py --require-locked-python")
    assert verification.index("--require-hashes -r requirements-py313.lock") < locked_step
    assert "sha256sum -c evidence/formal/SHA256SUMS" in verification  # recorded Lean evidence only
