"""Static policy: a failing fixture must fail the production quality gate."""
from importlib import util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = util.spec_from_file_location("vater_static_policy", ROOT / "scripts" / "check_static_policy.py")
assert spec is not None and spec.loader is not None
import sys
module = util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


@pytest.mark.parametrize("source,rule", [
    ("eval('2+2')", "STATIC-002-DANGEROUS-CALL"),
    ("exec('pass')", "STATIC-002-DANGEROUS-CALL"),
    ("from builtins import eval as unsafe; unsafe('3')", "STATIC-002-DANGEROUS-CALL"),
    ("import builtins as b; b.exec('pass')", "STATIC-002-DANGEROUS-CALL"),
    ("import os; os.system('echo hello')", "STATIC-002-DANGEROUS-CALL"),
    ("from os import popen as shell; shell('echo')", "STATIC-002-DANGEROUS-CALL"),
    ("import pickle; pickle.loads(b'data')", "STATIC-002-DANGEROUS-CALL"),
    ("from subprocess import run as process; process(['echo'], shell=True)", "STATIC-003-SHELL"),
    ("import subprocess as sp; sp.run(['echo'], shell=flag)", "STATIC-003-SHELL"),
    ("import subprocess; subprocess.Popen(['echo'], **options)", "STATIC-003-UNKNOWN-KEYWORDS"),
    ("from os import *", "STATIC-001-WILDCARD"),
    ("def broken(", "STATIC-001-SYNTAX"),
])
def test_reject_unsafe_construct(source, rule):
    got = module.inspect_python(source, file="core.py", core=True)
    assert rule in {item.rule for item in got}


@pytest.mark.parametrize("source", [
    "def add(left, right): return left+right",
    "import subprocess; subprocess.run(['echo'], shell=False)",
    "import subprocess; subprocess.run(['echo'])",
    "text = \"exec('not executed')\"",
    "def checked_add(x: int) -> int: return x + 1",
])
def test_allow_safe_construct(source):
    assert module.inspect_python(source, file="core.py", core=True) == []


def test_production_core_rejects_sdk_import_but_server_can_import():
    sample = "from mcp.server import Server\n"
    assert "STATIC-004-MCP-CORE" in {
        f.rule for f in module.inspect_python(sample, file="core.py", core=True)
    }
    assert module.inspect_python(sample, file="server_mcp.py", core=False) == []


def test_real_project_has_no_rule_violations():
    findings, count = module.check_project(ROOT)
    assert count >= 10
    assert findings == []


def test_missing_project_fails_without_false_green(tmp_path):
    with pytest.raises(ValueError, match="STATIC-000"):
        module.check_project(tmp_path)
    assert module.main(["--root", str(tmp_path)]) == 1


def test_cli_machine_readable_output(capsys):
    assert module.main(["--root", str(ROOT)]) == 0
    import json
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "pass"
    assert result["files_scanned"] >= 10
    assert result["findings"] == []


def test_line_numbers_and_stable_rule():
    found = module.inspect_python("x = 1\nexec('pass')\n", file="x.py", core=True)
    assert [(f.line, f.rule) for f in found] == [(2, "STATIC-002-DANGEROUS-CALL")]
