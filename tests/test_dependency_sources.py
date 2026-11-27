"""task.operations.004: the source and version of every dependency are fixed and clear.

docs/dependency_sources.json records, for each locked package, its exact
version, how many artifact hashes pin it, and the chain that adopted it
(direct requirement or "via" another package), plus every dependency install
in the workflows. These tests require the record to equal what the lock and
the workflows say now, every direct requirement to be satisfied by the lock,
the lock to name no other source, and every install that is not hash-locked
to be classified and explained.
"""
from __future__ import annotations

from importlib import util
import json
from pathlib import Path
import tomllib

from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).resolve().parents[1]
spec = util.spec_from_file_location("dependency_sources", ROOT / "scripts" / "dependency_sources.py")
sources = util.module_from_spec(spec)
spec.loader.exec_module(sources)
RECORD = json.loads((ROOT / "docs" / "dependency_sources.json").read_text(encoding="utf-8"))


def test_record_equals_the_lock_and_the_workflows():
    assert RECORD == sources.build()


def test_every_package_is_pinned_by_version_and_hash_and_has_an_adoption_route():
    for package in RECORD["packages"]:
        assert package["version"] and package["hashes"] >= 1, package
        assert package["adopted_via"], package["name"]
    assert any("verified-execution-prototype (pyproject.toml)" in p["adopted_via"] for p in RECORD["packages"])


def test_direct_requirements_match_pyproject_and_the_lock():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    declared = [*project["dependencies"], *(r for group in project["optional-dependencies"].values() for r in group)]
    parsed = {}
    for requirement in declared:
        name = requirement.split(">", 1)[0].split("=", 1)[0].split("<", 1)[0].strip()
        parsed[name] = requirement[len(name):]
    assert parsed == RECORD["direct_requirements"]
    locked = {p["name"]: p["version"] for p in RECORD["packages"]}
    for name, specifier in parsed.items():
        assert locked[name] in SpecifierSet(specifier), (name, locked[name], specifier)


def test_the_lock_names_no_other_source():
    text = (ROOT / "requirements-py313.lock").read_text()
    for directive in ("--index-url", "--extra-index-url", "--find-links", "-e ", "git+", "file:", "http://", "https://"):
        assert directive not in text, directive


def test_every_install_is_hash_locked_or_explained():
    kinds = {install["kind"] for install in RECORD["installs"]}
    assert kinds <= {"hash_locked", "tool_pinned", "diagnostic_unpinned"}
    release = [i for i in RECORD["installs"] if i["workflow"] == "python-verification.yml" and i["kind"] == "hash_locked"]
    assert release, "the verification workflow must install the hash-locked set"
    for install in RECORD["installs"]:
        assert install["reason"], install
