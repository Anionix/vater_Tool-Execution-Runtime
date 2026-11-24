"""Contract checks for the fixed local MCP launch profile."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("local_mcp_launcher", ROOT / "scripts/run_local_mcp.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_launcher_uses_host_owned_bounds():
    command, environment = launcher.launch_spec(ROOT, sys.executable)
    limits = json.loads((ROOT / "config/limits.json").read_text())
    for flag, field in [
        ("--max-bytes", "request_input_max_bytes"),
        ("--max-depth", "json_max_depth"),
        ("--max-output-bytes", "request_output_max_bytes"),
        ("--max-output-depth", "json_max_depth"),
    ]:
        assert command[command.index(flag) + 1] == str(limits[field])
    assert command[0:3] == [sys.executable, "-I", "-u"]
    assert command[command.index("--") + 1:] == [
        sys.executable, "-u", str(ROOT / "server_mcp.py")
    ]
    assert environment["PYTHONPATH"] == str(ROOT / "src")


def test_launcher_does_not_inherit_environment(monkeypatch):
    monkeypatch.setenv("VERIFIED_EXECUTION_LEDGER_PATH", "/unexpected.sqlite")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret")
    monkeypatch.setenv("PYTHONPATH", "/unexpected-imports")
    _, environment = launcher.launch_spec(ROOT, sys.executable)
    assert set(environment) == {
        "PYTHONPATH", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE", "LANG",
    }
    assert environment["PYTHONPATH"] == str(ROOT / "src")


def test_invalid_policy_fails_closed(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "limits.json").write_text("{}")
    with pytest.raises(launcher.PolicyConfigurationError):
        launcher.launch_spec(tmp_path, sys.executable)


def test_arguments_cannot_replace_child_command(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected process launch")
    monkeypatch.setattr(launcher.os, "execve", forbidden)
    assert launcher.main(["--", "arbitrary-command"]) == 2


def test_exec_failure_returns_stable_status(monkeypatch, capsys):
    def failed(*args, **kwargs):
        raise OSError("synthetic-secret")
    monkeypatch.setattr(launcher.os, "execve", failed)
    assert launcher.main([]) == 1
    assert capsys.readouterr().err == "LOCAL_LAUNCH_REJECTED\n"
