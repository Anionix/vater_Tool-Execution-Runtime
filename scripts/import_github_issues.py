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
from contextlib import contextmanager

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


# IMPORT-LOCK-001: an atomic Git reference gives participating importers a
# repository-wide single-writer lock. A crash can leave a stale lock branch,
# which MUST be inspected manually instead of silently overridden.
LOCK_REF = "refs/heads/vater-issue-import-lock"


@contextmanager
def repository_import_lock(repo: str):
    branch = gh("repo", "view", repo, "--json", "defaultBranchRef",
                "--jq", ".defaultBranchRef.name").strip()
    if not branch:
        raise RuntimeError("DEFAULT_BRANCH_UNKNOWN")
    head = gh("api", f"repos/{repo}/git/ref/heads/{branch}",
              "--jq", ".object.sha").strip()
    if not re.fullmatch(r"[a-f0-9]{40}", head):
        raise RuntimeError("DEFAULT_BRANCH_SHA_INVALID")
    try:
        gh("api", "-X", "POST", f"repos/{repo}/git/refs",
           "-f", f"ref={LOCK_REF}", "-f", f"sha={head}")
    except RuntimeError as exc:
        raise RuntimeError("IMPORT_LOCK_UNAVAILABLE: inspect stale Git branch") from exc
    try:
        yield
    finally:
        gh("api", "-X", "DELETE", f"repos/{repo}/git/{LOCK_REF}")


def remote_issue_identifiers(repo: str) -> set[str]:
    """IMPORT-002: query open AND closed issues by immutable task identifier."""
    remote = json.loads(gh("issue", "list", "--repo", repo, "--state", "all",
                           "--limit", "1000", "--json", "title"))
    return {task for item in remote
            if (task := task_identifier(item["title"])) is not None}


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
    # IMPORT-003: do not use a stale snapshot when multiple writers could race.
    # The atomic Git ref locks cooperating importers; an independent GitHub
    # client can still create the same task, so a fresh remote check is required.
    created = 0
    with repository_import_lock(args.repo):
        for item in candidates:
            if created >= args.limit:
                break
            if item["task_id"] in remote_issue_identifiers(args.repo):
                continue
            url = gh("issue", "create", "--repo", args.repo,
                     "--title", item["title"], "--body", item["body"]).strip()
            print(item["task_id"], url, flush=True)
            created += 1
        after = remote_issue_identifiers(args.repo)
    print(json.dumps({"created": created,
                      "already_registered": sum(x["task_id"] in after for x in candidates),
                      "remaining": sum(x["task_id"] not in after for x in candidates)},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
