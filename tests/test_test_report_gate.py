"""The official SDK suite must not pass by silently skipping tests."""
from __future__ import annotations
from importlib import util
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = util.spec_from_file_location("vater_test_report_gate", ROOT / "scripts/check_test_report.py")
assert SPEC and SPEC.loader
m = util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)


def write_report(path, *, tests=14, broken=None):
    suite = ET.Element("testsuite", tests=str(tests), failures=str(int(broken == "failure")),
                       errors=str(int(broken == "error")), skipped=str(int(broken == "skipped")))
    for i in range(tests):
        case = ET.SubElement(suite, "testcase", classname="test_sdk", name=f"test_{i}")
        if i == 0 and broken:
            ET.SubElement(case, broken)
    ET.ElementTree(suite).write(path, encoding="utf-8")


def test_valid_real_suite_report(tmp_path):
    p = tmp_path / "real.xml"
    write_report(p)
    assert m.check_report(p, minimum=14)["passed"] == 14
    assert m.main([str(p), "--minimum", "14"]) == 0


@pytest.mark.parametrize("broken", ["failure", "error", "skipped"])
def test_rejects_unsuccessful_testcase(tmp_path, broken):
    p = tmp_path / "unverified.xml"
    write_report(p, broken=broken)
    with pytest.raises(ValueError, match="NOT_ALL_TESTS_PASSED"):
        m.check_report(p, minimum=14)
    assert m.main([str(p), "--minimum", "14"]) == 1


def test_rejects_missing_testcases(tmp_path):
    p = tmp_path / "short.xml"
    write_report(p, tests=13)
    with pytest.raises(ValueError, match="INSUFFICIENT_TESTS"):
        m.check_report(p, minimum=14)


def test_rejects_forged_suite_counts(tmp_path):
    p = tmp_path / "mismatch.xml"
    write_report(p)
    root = ET.parse(p)
    root.getroot().set("tests", "100")
    root.write(p)
    with pytest.raises(ValueError, match="INCONSISTENT_SUITE_TOTALS"):
        m.check_report(p, minimum=14)


@pytest.mark.parametrize("contents", [b"", b"<unrelated/>", b"<testsuite"])
def test_rejects_invalid_report(tmp_path, contents):
    p = tmp_path / "invalid.xml"
    p.write_bytes(contents)
    with pytest.raises(ValueError):
        m.check_report(p, minimum=14)


def test_missing_file_fails(tmp_path):
    assert m.main([str(tmp_path / "absent.xml"), "--minimum", "14"]) == 1


def test_large_report_rejected(tmp_path):
    p = tmp_path / "huge.xml"
    p.write_bytes(b"x" * 8_000_001)
    with pytest.raises(ValueError, match="REPORT_TOO_LARGE"):
        m.check_report(p, minimum=14)


def test_rejects_orphan_testcases_without_suite(tmp_path):
    p = tmp_path / "orphan.xml"
    p.write_text("<testsuites>" + "<testcase classname=\"fake\" name=\"case\"/>" * 14 + "</testsuites>")
    with pytest.raises(ValueError, match="MISSING_TEST_SUITE"):
        m.check_report(p, minimum=14)
