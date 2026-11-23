"""Machine-check the distinction between diagnostic and release validation.

CI-CONTRACT-001: no official MCP PASS can come from skipped tests.
CI-CONTRACT-002: release verification still requires hash-pinned dependencies.
CI-CONTRACT-003: the diagnostic path is clearly labeled non-release.
These tests inspect checked-in workflow commands; they do not execute GitHub Actions.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_release_workflow_blocks_unlocked_dependencies():
    workflow = read("python-verification.yml")
    assert "python scripts/check_dependency_lock.py requirements-py313.lock" in workflow
    assert "pip install --require-hashes -r requirements-py313.lock" in workflow
    assert "python scripts/check_test_report.py official-mcp-report.xml --minimum 14" in workflow


def test_candidate_workflow_checks_hashes_and_real_tests():
    workflow = read("dependency-lock-candidate.yml")
    assert "uv pip compile pyproject.toml" in workflow
    assert "--generate-hashes" in workflow
    assert "--require-hashes" in workflow
    assert "check_dependency_lock.py requirements-py313.lock" in workflow
    assert "check_test_report.py official-mcp-report.xml --minimum 14" in workflow


def test_smoke_workflow_is_diagnostic_not_release():
    workflow = read("official-mcp-smoke.yml")
    assert "Diagnostic only - NOT a release gate" in workflow
    assert "mcp') == '2.3.0'" in workflow
    assert "tests/test_official_mcp_sdk.py" in workflow
    assert "python scripts/check_test_report.py official-mcp-smoke.xml --minimum 14" in workflow
    assert "if: always()" in workflow
    assert "retention-days: 7" in workflow


def test_smoke_workflow_has_no_write_access_or_failure_bypass():
    workflow = read("official-mcp-smoke.yml")
    assert "permissions:\n  contents: read" in workflow
    assert "persist-credentials: false" in workflow
    assert "continue-on-error: true" not in workflow
    assert "|| true" not in workflow


def test_test_count_is_grounded_in_collected_sdk_tests():
    source = (ROOT / "tests" / "test_official_mcp_sdk.py").read_text(encoding="utf-8")
    # A conservative review signal; collections still need actual runtime evidence.
    assert source.count("def test_sdk_") >= 11
    assert source.count("@pytest.mark.parametrize") >= 3
    assert "pytest.mark.skipif" in source
