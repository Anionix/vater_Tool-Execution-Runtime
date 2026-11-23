#!/usr/bin/env python3
"""Import GitHub Issues by immutable task ID, without duplicates.

States: not_created -> proposed -> remote_checked -> created.
Never mark a task complete or close an Issue during import. Existing issues
are identified by their bracketed task_id, NOT by their mutable title.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
TASK_ID = re.compile(r"^\[((?:task|gate)\.[A-Za-z0-9_.-]+)\]")


def task_identifier(title: str) -> str | None:
    match = TASK_ID.match(title)
    return match.group(1) if match else None


def load_candidates(path: Path) -> list[dict]:
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    identifiers = [item["task_id"] for item in entries]
    if len(identifiers) != 351 or len(set(identifiers)) != 351:
        raise ValueError("351_UNIQUE_TASK_IDS_REQUIRED")
    for item in entries:
        if task_identifier(item["title"]) != item["task_id"]:
            raise ValueError("INVALID_ISSUE_TITLE_ID")
        if not isinstance(item.get("body"), str):
            raise ValueError("INVALID_ISSUE_BODY")
    return entries


def missing_candidates(candidates: list[dict], remote_titles: list[str]) -> list[dict]:
    present = {x for title in remote_titles if (x := task_identifier(title)) is not None}
    return [entry for entry in candidates if entry["task_id"] not in present]


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False, timeout=90)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "GITHUB_COMMAND_FAILED")
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--execute", action="store_true", help="Require explicit approval to create issues")
    parser.add_argument("--all", action="store_true", help="Include all 351 tasks")
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("invalid owner/repo")
    if not 1 <= args.limit <= 351:
        parser.error("invalid limit")
    candidates = load_candidates(ROOT / "backlog" / "github_issues.jsonl")
    if not args.all:
        candidates = [x for x in candidates if x["kind"] == "gate" or x["phase"] == "trust" or x["task_id"] == "task.scope.015"]
    if not args.execute:
        print(json.dumps({"mode": "dry-run", "candidates": len(candidates), "first": [x["task_id"] for x in candidates[:args.limit]]}, ensure_ascii=False, indent=2))
        return
    remote = json.loads(gh("issue", "list", "--repo", args.repo, "--state", "all", "--limit", "1000", "--json", "title"))
    missing = missing_candidates(candidates, [x["title"] for x in remote])
    for item in missing[:args.limit]:
        url = gh("issue", "create", "--repo", args.repo, "--title", item["title"], "--body", item["body"]).strip()
        print(item["task_id"], url, flush=True)
    print(json.dumps({"created": min(args.limit, len(missing)), "already_registered": len(candidates) - len(missing), "remaining": max(0, len(missing) - args.limit)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
