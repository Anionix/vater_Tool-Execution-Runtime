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



def test_external_change_evidence_runs_only_on_a_person_s_request():
    """It writes issues in this repository, so nothing automatic may start it."""
    import re
    text = read("external-change-evidence.yml")
    triggers = text.split("\non:\n", 1)[1].split("\n\n", 1)[0]
    assert triggers == "  workflow_dispatch:\n  pull_request:\n    types: [labeled]"
    assert ("if: github.event_name == 'workflow_dispatch' || "
            "github.event.label.name == 'run-external-evidence'") in text
    assert "permissions:\n  contents: read\n  issues: write\n\n" in text
    assert text.count("permissions:") == 1
    assert "continue-on-error" not in text and "|| true" not in text
    assert "persist-credentials: false" in text and "pip install" not in text
    uses = re.findall(r"uses: ([^@\s]+)@(\S+)", text)
    assert uses and all(re.fullmatch(r"[0-9a-f]{40}", ref) for _action, ref in uses), uses


def test_no_plain_run_command_breaks_the_workflow_yaml():
    """A plain scalar cannot contain ': ' or end in ':' (YAML would read a mapping and
    GitHub would reject the whole workflow); such commands must be quoted or folded."""
    import re
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = re.match(r"^\s*(?:- )?run:\s*(.*)$", line)
            if not match or match.group(1)[:1] in ("", ">", "|", "'", '"'):
                continue
            value = match.group(1).split(" #", 1)[0]
            assert ": " not in value and not value.endswith(":"), f"{path.name}:{number}"
