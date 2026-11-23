"""Deterministic source manifest: never include this output in itself."""
from __future__ import annotations
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_NAMES = {"SHA256SUMS.txt", "file_sha256.jsonl"}
files = sorted(path for path in ROOT.rglob("*") if path.is_file()
               and not set(path.parts).intersection({"__pycache__", ".pytest_cache"})
               and path.name not in EXCLUDE_NAMES
               and path.suffix not in {".pyc"})
rows = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(ROOT)}"
        for path in files]
(ROOT / "SHA256SUMS.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")
print(f"Manifest records: {len(rows)}")
