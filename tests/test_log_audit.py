"""task.operations.012: logs are audited mechanically and a secret fails the audit.

scripts/audit_logs.py scans text files for credential shapes and for the
literal values of named environment variables. These tests plant each kind of
secret and require a failing audit with a redacted report; check that
look-alike placeholders pass; and audit the repository's evidence and the
real MCP server's diagnostics while it handles calls that carry a secret.
"""
from __future__ import annotations

from importlib import util
import json
from pathlib import Path

import pytest

from adapter_drivers import load_mcp_adapter, with_sdk_client

ROOT = Path(__file__).resolve().parents[1]
spec = util.spec_from_file_location("audit_logs", ROOT / "scripts" / "audit_logs.py")
audit_logs = util.module_from_spec(spec)
spec.loader.exec_module(audit_logs)

PLANTED = {
    "github_token": "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "stripe_like_key": "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
    "aws_access_key": "AKIA" + "IOSFODNN7EXAMPLE",
    "bearer_header": "Authorization: Bearer " + "abcDEF123456ghiJKL789012mno",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----",
    "jwt": "eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0" + ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "credential_assignment": "api_key=" + "Zm9vYmFyYmF6cXV4",
}


@pytest.mark.parametrize("rule", sorted(PLANTED))
def test_each_secret_shape_fails_the_audit_without_being_repeated(rule, tmp_path, capsys):
    log = tmp_path / "server.log"
    log.write_text(f"2026-11-27 call ok\nleaked {PLANTED[rule]} here\n", encoding="utf-8")
    assert audit_logs.main([str(tmp_path)]) == 1
    output = capsys.readouterr().out
    report = json.loads(output)
    assert [(f["rule"], f["line"]) for f in report["findings"]] == [(rule, 2)]
    assert PLANTED[rule] not in output  # the report is redacted


def test_a_named_secret_value_fails_the_audit(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VATER_LOCAL_HTTP_TOKEN", "plainHostToken" * 3)
    (tmp_path / "run.jsonl").write_text('{"note": "plainHostTokenplainHostTokenplainHostToken"}\n')
    assert audit_logs.main([str(tmp_path), "--secret-env", "VATER_LOCAL_HTTP_TOKEN"]) == 1
    assert json.loads(capsys.readouterr().out)["findings"][0]["rule"] == "known_secret_value"


def test_placeholders_and_ordinary_text_pass(tmp_path):
    (tmp_path / "ok.log").write_text("\n".join([
        "token: <redacted>", "password = ${DB_PASSWORD}", "api_key=REDACTED", "secret: null",
        "Bearer short", "ghp_tooShort", "the access token was refused (401)", "sk_live_x"]) + "\n")
    assert audit_logs.audit([tmp_path], [])["findings"] == []


def test_repository_evidence_and_docs_hold_no_secret():
    report = audit_logs.audit([ROOT / "evidence", ROOT / "docs"], [])
    assert report["files_scanned"] > 100 and report["findings"] == []


def test_server_diagnostics_do_not_log_a_secret_from_a_call(capfd):
    pytest.importorskip("mcp")
    secret = "sk_live_" + "auditPlantedValue12345"
    server = load_mcp_adapter("server_mcp_log_audit").build_server()

    async def scenario(client):
        await client.call_tool("checked_add", {"left": 1, "right": 2})
        await client.call_tool("checked_add", {"left": 1, "right": 2, "note": secret})
        await client.call_tool("delete_file", {"path": secret})

    with_sdk_client(server, scenario)
    captured = capfd.readouterr()
    diagnostics = captured.err + captured.out
    assert '"source":"mcp.tools.call"' in diagnostics  # the server did write its diagnostics
    assert audit_logs.audit_text(diagnostics, [secret]) == []
