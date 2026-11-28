"""task.operations.002: the tests a change runs follow from its diff, and a permission change always runs the safety suite.

scripts/select_tests.py maps changed paths to tests with config/test_selection.json.
These tests require every permission-bearing path (the launcher's pinned files,
risk classes, contracts, limits, workflow token permissions) to select the
safety suite, unexplained paths to select the whole suite, and documentation
or test-only changes to select just what they touch.
"""
from __future__ import annotations

from importlib import util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = util.spec_from_file_location("select_tests", ROOT / "scripts" / "select_tests.py")
selector = util.module_from_spec(spec)
spec.loader.exec_module(selector)
RULES = json.loads((ROOT / "config" / "test_selection.json").read_text())
LAUNCHER_PINS = sorted(json.loads((ROOT / "config" / "local_tool.json").read_text())["sha256"])
# Built at run time: this file must not itself "name" the paths it uses as examples.
RUNBOOK = "/".join(["docs", "runbooks", "disable_external_tools" + ".md"])
MONITOR = "scripts/" + "monitor" + "_operations.py"
UNNAMED = "scripts/" + "never" + "_named_by_a_test.py"
WORKFLOWS = sorted(f".github/workflows/{p.name}" for p in (ROOT / ".github/workflows").glob("*.yml"))


def test_the_safety_suite_exists():
    assert RULES["safety_suite"] and all((ROOT / test).is_file() for test in RULES["safety_suite"])


@pytest.mark.parametrize("path", [*LAUNCHER_PINS, "contracts/tool_risks.json", "config/local_tool.json",
                                  "config/limits.json", "contracts/github_issue_destination.json",
                                  "src/verified_execution/ledger.py", "src/verified_execution/kill_switch.py",
                                  "docs/permission_history.jsonl", *WORKFLOWS])
def test_every_permission_change_runs_the_safety_suite(path):
    selection = selector.select([path])
    assert selection["permission_change"] is True
    assert selection["safety_suite"] == sorted(RULES["safety_suite"])
    assert selection["full"] or set(RULES["safety_suite"]) <= set(selection["tests"])


def test_a_permission_change_hidden_among_documentation_still_runs_the_safety_suite():
    selection = selector.select([RUNBOOK, ".github/workflows/python-verification.yml"])
    assert selection["permission_change"] is True and selection["full"] is False
    assert set(RULES["safety_suite"]) <= set(selection["tests"])
    assert "tests/test_kill_switch.py" in selection["tests"]


def test_documentation_and_test_only_changes_run_only_what_they_touch():
    docs = selector.select([RUNBOOK])
    assert (docs["permission_change"], docs["full"], docs["tests"]) == (False, False, ["tests/test_kill_switch.py"])
    only_test = selector.select(["tests/test_log_audit.py"])
    assert only_test["tests"] == ["tests/test_log_audit.py"] and only_test["safety_suite"] == []
    script = selector.select([MONITOR])
    assert script["tests"] == ["tests/test_operation_monitoring.py"] and not script["full"]


@pytest.mark.parametrize("path", ["README.md", "something/new.txt", UNNAMED,
                                  "src/verified_execution/recorded_calculator.py", "pyproject.toml",
                                  "requirements-py313.lock", "tests/unit_inventory.json"])
def test_source_configuration_and_unexplained_paths_run_the_whole_suite(path):
    selection = selector.select([path])
    assert selection["full"] is True and selection["tests"] == ["tests"], selection["reasons"]


def test_an_empty_diff_never_selects_nothing():
    assert selector.select([])["tests"] == ["tests"]


def test_the_command_line_runs_the_safety_suite_by_name_even_with_the_whole_suite(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(selector.subprocess, "run", lambda command, cwd: ran.append(command) or
                        type("Done", (), {"returncode": 0})())
    assert selector.main(["contracts/tool_risks.json", "--run"]) == 0
    targets = ran[0][ran[0].index("-q") + 1:]
    assert "tests" in targets and set(RULES["safety_suite"]) <= set(targets)
    assert json.loads(capsys.readouterr().out)["permission_change"] is True
