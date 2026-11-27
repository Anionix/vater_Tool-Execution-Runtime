#!/usr/bin/env python3
"""task.operations.006 / 008: SHA-256 of the source, dependencies and Python artifacts; reproducible builds.

  python -m pip install --require-hashes --only-binary :all: -r requirements-build.lock
  python scripts/release_manifest.py --output release-manifest.json --dist dist

The manifest ties one commit's tracked source files (each file's SHA-256 and a
tree digest), the hash-locked runtime and build dependencies, and the built
sdist and wheel (SHA-256 and size) together. The artifacts are built three
times from fresh copies of the tracked files, with the hash-locked setuptools:
  A  SOURCE_DATE_EPOCH = commit time, TZ=UTC, umask 022
  B  SOURCE_DATE_EPOCH = commit time, TZ=Asia/Tokyo, umask 077, another path, other file times
  C  no SOURCE_DATE_EPOCH, TZ=UTC, umask 022
A and B are compared to see whether the build is reproducible when the
environment varies; A and C show what an uncontrolled build time changes.
Every differing archive member and field is recorded; the trial does not
fail the run, and the artifacts of A are the ones published.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
from importlib import metadata
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BUILD = ("import sys, setuptools.build_meta as backend; "
         "backend.build_sdist(sys.argv[1]); backend.build_wheel(sys.argv[1])")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def tracked_files(root: Path = ROOT) -> list[str]:
    return sorted(name for name in git(root, "ls-files", "-z").split("\0") if name and (root / name).is_file())


def source_section(root: Path = ROOT) -> dict:
    hashes = {name: sha256_bytes((root / name).read_bytes()) for name in tracked_files(root)}
    tree = sha256_bytes("".join(f"{digest}  {name}\n" for name, digest in hashes.items()).encode())
    return {"files": len(hashes), "tree_sha256": tree, "files_sha256": hashes}


def lock_section(lock: Path) -> dict:
    packages, current = [], None
    for line in lock.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==(\S+)", line)
        if match:
            current = {"name": match.group(1), "version": match.group(2), "sha256": []}
            packages.append(current)
        if current is not None:
            current["sha256"] += re.findall(r"--hash=sha256:([0-9a-f]{64})", line)
    return {"lock": lock.name, "lock_sha256": sha256_bytes(lock.read_bytes()), "packages": packages}


def artifacts(directory: Path) -> list[dict]:
    return [{"name": path.name, "sha256": sha256_bytes(path.read_bytes()), "bytes": path.stat().st_size}
            for path in sorted(directory.iterdir()) if path.suffix in (".whl", ".gz")]


def members(path: Path) -> dict[str, dict]:
    """Every archive member with its content hash and the metadata a build can vary."""
    found: dict[str, dict] = {}
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                found[info.filename] = {"sha256": sha256_bytes(archive.read(info)), "date_time": list(info.date_time),
                                        "external_attr": info.external_attr}
    else:
        raw = path.read_bytes()
        found["<gzip header>"] = {"mtime": int.from_bytes(raw[4:8], "little")}
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(raw))) as archive:
            for info in archive.getmembers():
                content = archive.extractfile(info).read() if info.isfile() else b""
                found[info.name] = {"sha256": sha256_bytes(content), "mtime": int(info.mtime), "mode": info.mode,
                                    "uid": info.uid, "gid": info.gid, "uname": info.uname, "gname": info.gname}
    return found


def differences(first: Path, second: Path) -> list[dict]:
    found = []
    names = sorted({p.name for p in first.iterdir()} | {p.name for p in second.iterdir()})
    for name in names:
        a, b = first / name, second / name
        if not a.exists() or not b.exists():
            found.append({"artifact": name, "member": None, "field": "present", "a": a.exists(), "b": b.exists()})
            continue
        if sha256_bytes(a.read_bytes()) == sha256_bytes(b.read_bytes()):
            continue
        left, right = members(a), members(b)
        for member in sorted(set(left) | set(right)):
            fields_a, fields_b = left.get(member, {}), right.get(member, {})
            for field in sorted(set(fields_a) | set(fields_b)):
                if fields_a.get(field) != fields_b.get(field):
                    found.append({"artifact": name, "member": member, "field": field,
                                  "a": fields_a.get(field), "b": fields_b.get(field)})
    return found


def copy_tracked(root: Path, target: Path, *, file_time: float | None = None) -> None:
    for name in tracked_files(root):
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, destination)
        if file_time is not None:
            os.utime(destination, (file_time, file_time))


def build(source: Path, out: Path, environment: dict, umask: int) -> None:
    out.mkdir(parents=True)
    subprocess.run([sys.executable, "-c", BUILD, str(out)], cwd=source, env=environment, check=True,
                   capture_output=True, preexec_fn=lambda: os.umask(umask))


def trial(root: Path, work: Path) -> dict:
    epoch = git(root, "log", "-1", "--format=%ct").strip()
    base = {key: value for key, value in os.environ.items() if key not in ("SOURCE_DATE_EPOCH", "TZ")}
    variants = {
        "A": {"SOURCE_DATE_EPOCH": epoch, "TZ": "UTC", "umask": 0o022, "path": "a/src", "file_time": None},
        "B": {"SOURCE_DATE_EPOCH": epoch, "TZ": "Asia/Tokyo", "umask": 0o077, "path": "b/another/path/src",
              "file_time": time.time() - 86_400},
        "C": {"SOURCE_DATE_EPOCH": None, "TZ": "UTC", "umask": 0o022, "path": "c/src", "file_time": None},
    }
    described = {}
    for name, variant in variants.items():
        source = work / variant["path"]
        copy_tracked(root, source, file_time=variant["file_time"])
        environment = dict(base, TZ=variant["TZ"])
        if variant["SOURCE_DATE_EPOCH"] is not None:
            environment["SOURCE_DATE_EPOCH"] = variant["SOURCE_DATE_EPOCH"]
        build(source, work / f"dist-{name}", environment, variant["umask"])
        described[name] = {"SOURCE_DATE_EPOCH": variant["SOURCE_DATE_EPOCH"], "TZ": variant["TZ"],
                           "umask": oct(variant["umask"]), "build_path_depth": len(Path(variant["path"]).parts),
                           "file_times": "one day earlier" if variant["file_time"] else "time of copy",
                           "artifacts": artifacts(work / f"dist-{name}")}
    controlled = differences(work / "dist-A", work / "dist-B")
    return {"variants": described, "controlled_identical": controlled == [],
            "controlled_differences": controlled,
            "without_source_date_epoch": differences(work / "dist-A", work / "dist-C")}


def manifest(root: Path, work: Path) -> dict:
    reproducibility = trial(root, work)
    return {
        "commit": git(root, "rev-parse", "HEAD").strip(),
        "source": source_section(root),
        "dependencies": lock_section(root / "requirements-py313.lock"),
        "build_requirements": lock_section(root / "requirements-build.lock"),
        "artifacts": reproducibility["variants"]["A"]["artifacts"],
        "environment": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                        "platform": platform.platform(), "setuptools": metadata.version("setuptools")},
        "reproducibility": reproducibility,
    }


def summary(full: dict) -> dict:
    """The manifest without the per-file source list (kept in the artifact)."""
    return {**full, "source": {key: value for key, value in full["source"].items() if key != "files_sha256"}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dist", type=Path, required=True)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="vater-build-") as directory:
        work = Path(directory)
        full = manifest(ROOT, work)
        args.dist.mkdir(parents=True, exist_ok=True)
        for path in (work / "dist-A").iterdir():
            shutil.copyfile(path, args.dist / path.name)
    args.output.write_text(json.dumps(full, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(summary(full), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
