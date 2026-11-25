#!/usr/bin/env python3
"""Close canonical GitHub issues for completed tasks on the default branch."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from urllib.parse import quote

from import_github_issues import ROOT, gh, task_identifier

LEDGER = "backlog/tasks.jsonl"
INDEX = "backlog/ISSUE_INDEX.jsonl"


def git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def read_json_lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def completed_numbers(tasks: list[dict], index: list[dict], blob: str, repo: str) -> dict[int, str]:
    by_identifier = {task["task_id"]: task for task in tasks}
    if len(by_identifier) != len(tasks) or len(index) != len(tasks):
        raise ValueError("Task ledger and issue index have different coverage")
    missing = set(by_identifier)
    numbers = set()
    completed = {}
    for row in index:
        identifier = row["task_id"]
        number = row["issue_number"]
        expected_url = f"https://github.com/{repo}/issues/{number}"
        if (identifier not in missing or type(number) is not int or number < 1
                or number in numbers or row["issue_url"].casefold() != expected_url.casefold()):
            raise ValueError(f"Invalid canonical issue mapping: {identifier}")
        missing.remove(identifier)
        numbers.add(number)
        task = by_identifier[identifier]
        if (row["ledger_git_blob_sha"] != blob or row["task_status"] != task["status"]
                or row["verification_status"] != task["verification_status"]):
            raise ValueError(f"Issue index is stale: {identifier}")
        if task["status"] == "done":
            completed[number] = identifier
    return completed


def pending_closures(completed: dict[int, str], issues: list[dict]) -> dict[int, str]:
    remote = {issue["number"]: issue for issue in issues}
    pending = {}
    for number, identifier in completed.items():
        issue = remote.get(number)
        if issue is None or task_identifier(issue["title"]) != identifier:
            raise ValueError(f"GitHub issue #{number} does not match {identifier}")
        if issue["state"] == "open":
            pending[number] = identifier
        elif issue["state"] != "closed":
            raise ValueError(f"Unexpected state for GitHub issue #{number}")
    return pending


def require_current_default_branch(endpoint: str) -> None:
    branch = gh("api", endpoint, "--jq", ".default_branch").strip()
    branch_path = quote(branch, safe="/")
    remote = gh("api", f"{endpoint}/git/ref/heads/{branch_path}", "--jq", ".object.sha").strip()
    if remote != git("rev-parse", "HEAD") or git("status", "--porcelain"):
        raise ValueError("Apply requires a clean checkout of the current default branch")


def synchronize(repo: str, *, apply: bool) -> None:
    endpoint = f"repos/{repo}"
    subprocess.run([sys.executable, str(ROOT / "backlog/validate_tasks.py")], check=True)
    completed = completed_numbers(
        read_json_lines((ROOT / LEDGER).read_text(encoding="utf-8")),
        read_json_lines((ROOT / INDEX).read_text(encoding="utf-8")),
        git("hash-object", LEDGER), repo,
    )
    snapshot = gh("api", "--paginate", f"{endpoint}/issues?state=all&per_page=100",
                  "--jq", '.[] | select(has("pull_request") | not) | {number,title,state} | tojson')
    pending = pending_closures(completed, read_json_lines(snapshot))
    print(json.dumps({"completed": len(completed), "already_closed": len(completed) - len(pending),
                      "pending": pending, "apply": apply}), flush=True)
    if not apply or not pending:
        return
    require_current_default_branch(endpoint)

    # completed_issue_synchronization: verified_open -> closed; already_closed -> already_closed.
    # On a partial failure, the next run skips issues that GitHub already closed.
    for number, identifier in pending.items():
        result = json.loads(gh("api", "--method", "PATCH", f"{endpoint}/issues/{number}",
                               "-f", "state=closed", "-f", "state_reason=completed"))
        if (result["number"] != number or task_identifier(result["title"]) != identifier
                or result["state"] != "closed" or result["state_reason"] != "completed"):
            raise RuntimeError(f"GitHub did not confirm completion for issue #{number}")
        print(f"Closed #{number} {identifier}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub repository in owner/name form")
    parser.add_argument("--apply", action="store_true", help="Close the issues shown by preview mode")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("invalid owner/repo")
    try:
        synchronize(args.repo, apply=args.apply)
    except (KeyError, ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
