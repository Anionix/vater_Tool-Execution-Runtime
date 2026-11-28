"""gate.scope.completed: each scope task's acceptance criterion, checked against the current files.

The scope tasks were accepted on evidence from the original archives (tests and paths that no
longer exist here). These tests re-check each criterion against the repository as it is now,
so scripts/rebuild_scope_gate.py can rebuild the gate on executed checks; the README's and the
scope contract's wording still needs a person's re-check whenever they change.
"""
from __future__ import annotations

from importlib import util
import inspect
import json
from pathlib import Path
import re
import subprocess
import sys
import time

import anyio
import pytest

from verified_execution import core
from verified_execution.request_limits import BoundedReadOnlyExecutor, ReadOnlyPolicy

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
CONTRACT = (ROOT / "docs" / "SCOPE_CONTRACT.md").read_text(encoding="utf-8")
ARCHITECTURE = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
LIMITS = json.loads((ROOT / "config" / "limits.json").read_text(encoding="utf-8"))


def script(name: str):
    spec = util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "scripts"))
    spec.loader.exec_module(module)
    return module


def section(text: str, heading: str) -> str:
    return text.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]


def purpose(text: str) -> str:
    match = re.search(r"\*\*(?:目的：)?(Pythonの純粋な計算を[^*]+)\*\*", text)
    assert match, "no purpose sentence"
    return match.group(1)


def test_scope_001_one_purpose_sentence_is_identical_in_readme_and_design():
    sentences = {name: purpose(text) for name, text in
                 (("README.md", README), ("SCOPE_CONTRACT.md", CONTRACT), ("ARCHITECTURE.md", ARCHITECTURE))}
    assert len(set(sentences.values())) == 1, sentences
    assert next(iter(sentences.values())).count("。") == 1  # one sentence


def test_scope_002_single_local_user_is_distinct_from_authenticated_remote_publishing():
    assert "Only local single-user pure arithmetic belongs to the initial release" in README
    assert "認証付き遠隔公開" in section(CONTRACT, "収録しないもの（初期公開）")
    assert LIMITS["scope"].startswith("single-local-user")
    assert "初期版は単一ローカル利用者" in ARCHITECTURE


def test_scope_003_python_first_local_steps_without_rust():
    environment = section(CONTRACT, "初期環境")
    for step in ("Python **3.13以上**", "python3.13 -m venv .venv", "副作用のない `checked_add` のみ",
                 "PYTHONPATH=src python -m pytest -q tests/test_core.py"):
        assert step in environment, step
    assert 'requires-python = ">=3.13"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "Rustコンパイル" in section(CONTRACT, "収録しないもの（初期公開）")
    assert not (ROOT / "Cargo.toml").exists()  # the optional Rust port lives apart, in rust/


def test_scope_004_pure_and_official_mcp_tests_are_separate_and_other_entries_are_later_profiles():
    pure = (ROOT / "tests" / "test_core.py").read_text(encoding="utf-8")
    official = (ROOT / "tests" / "test_official_mcp_sdk.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import) mcp\b", pure, re.M) and re.search(r"\bmcp\b", official)
    workflow = (ROOT / ".github" / "workflows" / "python-verification.yml").read_text(encoding="utf-8")
    assert "python -m pytest -q -rs --junitxml=official-mcp-report.xml tests/test_official_mcp_sdk.py" in workflow
    assert "HTTP、WebMCP、他社モデルへの接続は初期版に含めない" in section(CONTRACT, "初期環境")


def test_scope_005_the_first_tool_is_side_effect_free_addition():
    source = inspect.getsource(core.checked_add)
    for effect in ("open(", "socket", "subprocess", "urllib", "requests", "write(", "os."):
        assert effect not in source, effect
    assert core.checked_add(2, 3) == 5
    assert "初期計算は副作用のない `checked_add` のみ" in CONTRACT


def test_scope_006_success_and_input_error_are_both_defined_in_the_contract():
    contract = json.loads((ROOT / "contracts" / "checked_add.output.contract.json").read_text(encoding="utf-8"))
    assert set(contract["domains"]) == {"success", "failure"}
    assert contract["domains"]["success"]["required"] == ["result"]
    assert "INVALID_ARGUMENT" in contract["domains"]["failure"]["error_code"]["enum"]
    assert '成功 `{result: 3}` と失敗 `{error_code: "INVALID_ARGUMENT"}`' in CONTRACT


def test_scope_007_the_2026_07_28_version_and_older_versions_are_tested_apart():
    official = (ROOT / "tests" / "test_official_mcp_sdk.py").read_text(encoding="utf-8")
    assert 'assert client.protocol_version == "2026-07-28"' in official
    assert 'assert client.protocol_version != "2026-07-28"' in official
    assert "新旧の標準入出力接続を別々に通す" in CONTRACT


def test_scope_008_result_correctness_and_protocol_conformance_have_separate_criteria():
    criteria = section(CONTRACT, "合格条件を5種類に分離")
    normal = re.search(r"1\. \*\*正常\*\*：(.+)", criteria).group(1)
    compatible = re.search(r"4\. \*\*互換\*\*：(.+)", criteria).group(1)
    assert "計算が正しい" in normal and "公式MCP開発キット" in compatible and normal != compatible


def test_scope_009_non_goals_name_remote_auth_payment_external_code_and_gpu():
    excluded = section(CONTRACT, "収録しないもの（初期公開）")
    for item in ("認証付き遠隔公開", "決済", "未信頼コードの実行", "GPU"):
        assert item in excluded, item


def test_scope_010_the_input_limit_is_in_bytes_in_the_configuration():
    assert type(LIMITS["request_input_max_bytes"]) is int and LIMITS["request_input_max_bytes"] == 4096
    assert "受信1行は最大 **4096バイト**" in CONTRACT


def test_scope_011_the_time_limit_is_per_request_and_checkable():
    assert LIMITS["readonly_request_timeout_ms"] == 2000 and "要求時間は **2000ミリ秒**" in CONTRACT
    ReadOnlyPolicy(timeout_ms=LIMITS["readonly_request_timeout_ms"], max_concurrent=LIMITS["readonly_max_concurrent"])
    executor = BoundedReadOnlyExecutor(ReadOnlyPolicy(timeout_ms=100, max_concurrent=1))

    async def calls():
        assert await executor.run(lambda: 5) == 5  # each request gets its own deadline
        with pytest.raises(TimeoutError):
            await executor.run(time.sleep, 0.5)

    anyio.run(calls)


def test_scope_012_the_concurrency_limit_and_its_change_conditions_are_written():
    assert LIMITS["readonly_max_concurrent"] == 1 and "同時実行は **1件**" in CONTRACT
    assert "同時実行上限を増やせるのは" in section(CONTRACT, "量・時間・同時実行の上限")


def test_scope_013_a_test_path_without_paid_models_exists():
    mock = (ROOT / "src" / "verified_execution" / "model_mock.py").read_text(encoding="utf-8")
    for network in ("urllib", "http.client", "socket", "requests", "httpx", "api_key"):
        assert network not in mock, network
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider",
                             "tests/test_model_mock.py"], cwd=ROOT, capture_output=True, text=True, timeout=300,
                            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src")})
    assert result.returncode == 0, result.stdout[-2000:]  # no API key in the environment, and it passes


def test_scope_014_five_success_criteria_are_fixed():
    criteria = re.findall(r"^\d\. \*\*(.+?)\*\*", section(CONTRACT, "合格条件を5種類に分離"), re.M)
    assert criteria == ["正常", "異常", "権限", "互換", "再現性"]


def test_scope_015_python_and_the_official_package_are_pinned_and_formal_tools_separately():
    lock = (ROOT / "requirements-py313.lock").read_text(encoding="utf-8")
    checked = script("check_dependency_lock").validate_lock(lock)
    assert len(checked) == 33 and checked["mcp"] == "2.3.0"
    assert len(re.findall(r"--hash=sha256:", lock)) == 485
    assert "Rust、Lean、Kaniは採用時に別途固定する" in CONTRACT
    workflow = (ROOT / ".github" / "workflows" / "python-verification.yml").read_text(encoding="utf-8")
    assert "cargo install --locked kani-verifier --version 0.68.0" in workflow  # pinned when adopted
    assert (ROOT / "rust" / "vater_core" / "Cargo.lock").is_file()


def test_scope_016_unexecuted_tests_cannot_be_shown_as_success(tmp_path):
    assert "失敗・未実行を成功へ書き換えない" in CONTRACT
    report = tmp_path / "skipped.xml"
    report.write_text('<testsuite tests="2" failures="0" errors="0" skipped="1">'
                      '<testcase classname="t" name="ran"/>'
                      '<testcase classname="t" name="did_not_run"><skipped/></testcase></testsuite>')
    with pytest.raises(ValueError):
        script("check_test_report").check_report(report, minimum=1)


def test_scope_017_the_old_prototype_s_unexecuted_rust_lean_kani_are_noted():
    changelog = (ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Rustプログラム、Lean/Kaniの形式証明が実行されたわけではない" in changelog
    provenance = (ROOT / "docs" / "PROVENANCE.md").read_text(encoding="utf-8")
    assert "do **not** treat these historical checksums as hashes of the new GitHub commit" in provenance


def test_scope_018_task_identifiers_are_not_reused_and_changes_are_recorded():
    rows = [json.loads(line) for line in (ROOT / "backlog" / "tasks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 351 and len({row["task_id"] for row in rows}) == 351
    events = [json.loads(line) for line in (ROOT / "backlog" / "progress_events.jsonl").read_text(encoding="utf-8")
              .splitlines() if line.strip()]
    assert events and all({"task_id", "from_status", "to_status"} <= set(event) for event in events)
    assert {event["task_id"] for event in events} <= {row["task_id"] for row in rows}
    assert (ROOT / "backlog" / "migration_events.jsonl").is_file()
    validated = subprocess.run([sys.executable, "backlog/validate_tasks.py"], cwd=ROOT, capture_output=True,
                               text=True, timeout=120)
    assert validated.returncode == 0, validated.stdout + validated.stderr
    assert "全351件の `task_id` は不変" in CONTRACT
