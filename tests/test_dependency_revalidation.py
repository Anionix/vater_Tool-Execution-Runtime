"""task.operations.014: a dependency update is revalidated with every required check on the new versions.

scripts/required_checks.py takes the required checks from the release
verification workflow itself and runs them with the interpreter of a
candidate environment. The lock-candidate workflow resolves the newest
allowed versions (on dependency changes and weekly), installs them by hash
and runs every required check there before a person reviews the candidate.
"""
from __future__ import annotations

from importlib import util
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = util.spec_from_file_location("required_checks", ROOT / "scripts" / "required_checks.py")
required = util.module_from_spec(spec)
spec.loader.exec_module(required)
RELEASE = (ROOT / ".github/workflows/python-verification.yml").read_text()
CANDIDATE = (ROOT / ".github/workflows/dependency-lock-candidate.yml").read_text()


def test_the_required_checks_are_every_run_step_of_the_release_verification_except_installs():
    commands = [step["command"] for step in required.required_commands()]
    steps = required.run_steps(RELEASE)
    assert len(steps) == len(re.findall(r"^\s*run:", RELEASE, flags=re.M))
    # Only installs, and the build job with its own hash-locked backend, are left out.
    assert required.INDEPENDENT_OF_RUNTIME_DEPENDENCIES == {"release-artifacts", "formal-rust"}
    assert commands == [step["command"] for step in steps if "pip install" not in step["command"]
                        and step["job"] not in {"release-artifacts", "formal-rust"}]
    assert len(commands) >= 25
    phases = re.findall(r"run: (python scripts/verify_phase\.py [a-z_]+(?: --completed-only)?)", RELEASE)
    assert phases and set(phases) <= set(commands)
    assert "python scripts/staging_trial.py --output staging-trial.json" in commands
    assert any(command.startswith("python -m pytest -q tests/test_adapter_boundary.py") for command in commands)
    assert not any("pip install" in command for command in commands)
    assert required.WORKFLOW == ROOT / ".github/workflows/python-verification.yml"  # one list, not a copy


def test_each_check_runs_with_the_candidate_interpreter():
    python = "/candidate/.venv/bin/python"
    assert required.with_interpreter("python -m pytest -q", python) == f"{python} -m pytest -q"
    assert (required.with_interpreter("python a.py && python b.py", python)
            == f"{python} a.py && {python} b.py")
    assert required.with_interpreter("sha256sum -c evidence/formal/SHA256SUMS", python) == \
        "sha256sum -c evidence/formal/SHA256SUMS"
    assert required.with_interpreter('python -c "import python_x"', python) == f'{python} -c "import python_x"'


def test_every_check_runs_and_any_failure_fails_the_revalidation(tmp_path, capsys):
    workflow = tmp_path / "workflow.yml"
    workflow.write_text("jobs:\n  candidate:\n    steps:\n"
                        "      - name: install\n        run: python -m pip install something\n"
                        "      - name: fails\n        run: python -c \"raise SystemExit(3)\"\n"
                        "      - name: passes after a failure\n        run: >-\n          python -c\n"
                        "          \"import sys; assert sys.executable\"\n")
    assert required.main(["--workflow", str(workflow)]) == 1
    summary = json.loads(capsys.readouterr().out.split("::endgroup::\n")[-1])
    assert summary["commands"] == 2 and summary["interpreter"] == sys.executable
    assert [(f["name"], f["returncode"]) for f in summary["failed"]] == [("fails", 3)]
    passing = tmp_path / "passing.yml"
    passing.write_text("jobs:\n  candidate:\n    steps:\n      - name: ok\n        run: python -c \"pass\"\n")
    assert required.main(["--workflow", str(passing)]) == 0
    empty = tmp_path / "empty.yml"
    empty.write_text("jobs: {}\n")
    assert required.main(["--workflow", str(empty)]) == 1  # no checks is never a pass


def test_the_candidate_workflow_runs_every_required_check_on_the_hash_installed_versions():
    install = CANDIDATE.index("uv pip sync requirements-py313.lock --python .venv/bin/python --require-hashes")
    record = CANDIDATE.index(".venv/bin/python scripts/dependency_sources.py --write")
    checks = CANDIDATE.index(".venv/bin/python scripts/required_checks.py")
    assert install < record < checks
    assert "schedule:\n    - cron: '23 4 * * 1'" in CANDIDATE
    assert "      - pyproject.toml\n" in CANDIDATE
    assert "            requirements-py313.lock\n            docs/dependency_sources.json\n" in CANDIDATE
    assert "continue-on-error" not in CANDIDATE and "|| true" not in CANDIDATE
