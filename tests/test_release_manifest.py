"""task.operations.006 / 008: source, dependency and artifact SHA-256 are traceable; builds are compared.

scripts/release_manifest.py records every tracked file's SHA-256 and a tree
digest, both hash-locked dependency sets and the built artifacts, then builds
three times under varied environments and records every differing archive
member and field. CI builds with the hash-locked setuptools; these offline
tests check the hashing and the comparison with synthetic archives and a fake
builder, so they never run a downloaded build backend.
"""
from __future__ import annotations

import gzip
import hashlib
from importlib import util
import io
from pathlib import Path
import re
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = util.spec_from_file_location("release_manifest", ROOT / "scripts" / "release_manifest.py")
release = util.module_from_spec(spec)
spec.loader.exec_module(release)
WORKFLOW = (ROOT / ".github/workflows/python-verification.yml").read_text()


def wheel(path: Path, *, date_time=(2026, 11, 27, 0, 0, 0), content=b"print('ok')\n", mode=0o644) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo("verified_execution/__init__.py", date_time=date_time)
        info.external_attr = mode << 16
        archive.writestr(info, content)


def sdist(path: Path, *, mtime=1_795_000_000, gzip_mtime=0, mode=0o644) -> None:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        data = b"[project]\nname = 'x'\n"
        info = tarfile.TarInfo("pkg-0.11.0/pyproject.toml")
        info.size, info.mtime, info.mode = len(data), mtime, mode
        archive.addfile(info, io.BytesIO(data))
    path.write_bytes(gzip.compress(raw.getvalue(), mtime=gzip_mtime))


def test_every_tracked_file_is_hashed_into_the_tree_digest():
    source = release.source_section(ROOT)
    listed = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    assert sorted(source["files_sha256"]) == sorted(name for name in listed if (ROOT / name).is_file())
    assert source["files_sha256"]["pyproject.toml"] == hashlib.sha256((ROOT / "pyproject.toml").read_bytes()).hexdigest()
    rows = "".join(f"{digest}  {name}\n" for name, digest in source["files_sha256"].items())
    assert source["tree_sha256"] == hashlib.sha256(rows.encode()).hexdigest()


def test_both_dependency_sets_are_recorded_by_version_and_artifact_hash():
    runtime = release.lock_section(ROOT / "requirements-py313.lock")
    build = release.lock_section(ROOT / "requirements-build.lock")
    assert runtime["lock_sha256"] == hashlib.sha256((ROOT / "requirements-py313.lock").read_bytes()).hexdigest()
    assert {p["name"]: p["version"] for p in runtime["packages"]}["mcp"] == "2.3.0"
    assert all(p["sha256"] for p in runtime["packages"])
    assert build["packages"] == [{"name": "setuptools", "version": "80.9.0", "sha256": [
        "062d34222ad13e0cc312a4c02d73f059e86a4acbfbdea8f8f76b28c99f306922"]}]


def test_identical_builds_have_no_differences_and_varied_ones_name_each_field(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    for directory in (a, b):
        wheel(directory / "pkg-0.11.0-py3-none-any.whl")
        sdist(directory / "pkg-0.11.0.tar.gz")
    assert release.differences(a, b) == []
    wheel(b / "pkg-0.11.0-py3-none-any.whl", date_time=(2026, 11, 28, 9, 0, 0), mode=0o600)
    sdist(b / "pkg-0.11.0.tar.gz", mtime=1_795_086_400, gzip_mtime=1_795_086_400)
    found = {(d["artifact"].split("-0.11.0")[1], d["member"], d["field"]) for d in release.differences(a, b)}
    assert found == {("-py3-none-any.whl", "verified_execution/__init__.py", "date_time"),
                     ("-py3-none-any.whl", "verified_execution/__init__.py", "external_attr"),
                     (".tar.gz", "<gzip header>", "mtime"), (".tar.gz", "pkg-0.11.0/pyproject.toml", "mtime")}
    wheel(b / "pkg-0.11.0-py3-none-any.whl", content=b"print('changed')\n")
    assert any(d["field"] == "sha256" for d in release.differences(a, b))
    (b / "pkg-0.11.0.tar.gz").unlink()
    assert {"artifact": "pkg-0.11.0.tar.gz", "member": None, "field": "present", "a": True, "b": False} in \
        release.differences(a, b)


def fake_builder(stamp_from_environment: bool):
    """Stands in for setuptools: deterministic, or stamping the wall clock when no epoch is set."""
    def build(source, out, environment, umask):
        out.mkdir(parents=True)
        epoch = environment.get("SOURCE_DATE_EPOCH")
        date_time = (2026, 11, 27, 0, 0, 0) if epoch or not stamp_from_environment else (2026, 11, 28, 12, 0, 0)
        wheel(out / "verified_execution_prototype-0.11.0-py3-none-any.whl", date_time=date_time)
        sdist(out / "verified_execution_prototype-0.11.0.tar.gz")
    return build


def test_the_manifest_ties_source_dependencies_and_artifacts_together(tmp_path, monkeypatch):
    monkeypatch.setattr(release, "build", fake_builder(stamp_from_environment=True))
    monkeypatch.setattr(release.metadata, "version", lambda name: "80.9.0")
    full = release.manifest(ROOT, tmp_path)
    assert full["commit"] == subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                            text=True, check=True).stdout.strip()
    assert full["source"]["files"] == len(full["source"]["files_sha256"]) > 100
    assert full["dependencies"]["lock"] == "requirements-py313.lock"
    assert full["build_requirements"]["lock"] == "requirements-build.lock"
    assert [a["name"] for a in full["artifacts"]] == ["verified_execution_prototype-0.11.0-py3-none-any.whl",
                                                      "verified_execution_prototype-0.11.0.tar.gz"]
    assert all(len(a["sha256"]) == 64 and a["bytes"] > 0 for a in full["artifacts"])
    trial = full["reproducibility"]
    assert trial["controlled_identical"] is True and trial["controlled_differences"] == []
    assert trial["variants"]["B"]["TZ"] == "Asia/Tokyo" and trial["variants"]["B"]["umask"] == "0o77"
    assert trial["variants"]["C"]["SOURCE_DATE_EPOCH"] is None
    assert [d["field"] for d in trial["without_source_date_epoch"]] == ["date_time"]  # recorded, not hidden
    short = release.summary(full)
    assert "files_sha256" not in short["source"]
    assert short["reproducibility"]["without_source_date_epoch"] == [{
        "artifact": "verified_execution_prototype-0.11.0-py3-none-any.whl", "field": "date_time", "members": 1,
        "example": {"member": "verified_execution/__init__.py", "a": [2026, 11, 27, 0, 0, 0],
                    "b": [2026, 11, 28, 12, 0, 0]}}]


def test_each_build_gets_a_fresh_copy_of_exactly_the_tracked_files(tmp_path):
    release.copy_tracked(ROOT, tmp_path / "copy", file_time=1_700_000_000)
    copied = sorted(str(p.relative_to(tmp_path / "copy")) for p in (tmp_path / "copy").rglob("*") if p.is_file())
    assert copied == release.tracked_files(ROOT)
    assert (tmp_path / "copy" / "pyproject.toml").stat().st_mtime == 1_700_000_000


def test_ci_builds_with_the_hash_locked_backend_and_keeps_artifacts_and_manifest():
    job = re.split(r"\n  (?=[A-Za-z0-9_-]+:\n)", WORKFLOW.split("  release-artifacts:\n", 1)[1], maxsplit=1)[0]
    install = job.index("python -m pip install --require-hashes --only-binary :all: -r requirements-build.lock")
    assert install < job.index("python scripts/release_manifest.py --output release-manifest.json --dist dist")
    assert "dist/\n            release-manifest.json" in job and "retention-days: 90" in job
    assert "permissions:" not in job  # the workflow's read-only token
