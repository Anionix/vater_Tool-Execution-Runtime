#!/usr/bin/env python3
"""task.operations.013: every permission change leaves a history entry.

  python scripts/permission_history.py check
  python scripts/permission_history.py record --reason "..." --approved-by "..."
  python scripts/permission_history.py backfill        # rebuild from git history (needs full history)

A permission snapshot is derived from the files that grant or limit what runs:
tool risk classes and the minimum profile (contracts/tool_risks.json), the
launcher's approved code (config/local_tool.json), external destinations and
their credential scope, local limits (config/limits.json) and the GitHub token
permissions of every workflow. docs/permission_history.jsonl is a hash chain
of snapshots with the changes between them. 'check' fails when the chain was
edited or when the current files differ from the last recorded snapshot, so a
permission change cannot land without its history entry.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
HISTORY = Path("docs/permission_history.jsonl")
SOURCES = ["contracts/tool_risks.json", "config/local_tool.json", "config/limits.json",
           "contracts/github_issue_destination.json", ".github/workflows"]

Reader = Callable[[str], Optional[str]]


def file_reader(root: Path) -> Reader:
    def read(relative: str) -> str | None:
        path = root / relative
        return path.read_text(encoding="utf-8") if path.is_file() else None
    return read


def workflow_names(root: Path) -> list[str]:
    return sorted(f".github/workflows/{p.name}" for p in (root / ".github" / "workflows").glob("*.yml"))


def workflow_permissions(text: str) -> dict:
    """GitHub token permissions of the workflow and of each job (a small, strict reader)."""
    lines = text.splitlines()
    found: dict = {}
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)permissions:\s*(.*?)\s*(#.*)?$", line)
        if not match:
            continue
        indent, inline = len(match.group(1)), match.group(2)
        values: dict | str = inline if inline else {}
        if not inline:
            for following in lines[index + 1:]:
                if not following.strip() or following.lstrip().startswith("#"):
                    continue
                item = re.match(r"^(\s*)([A-Za-z-]+):\s*([A-Za-z-]+)\s*(#.*)?$", following)
                if not item or len(item.group(1)) <= indent:
                    break
                values[item.group(2)] = item.group(3)
        scope = "workflow"
        if indent:
            for previous in reversed(lines[:index]):
                owner = re.match(r"^(\s*)([A-Za-z0-9_-]+):\s*$", previous)
                if owner and len(owner.group(1)) == indent - 2:
                    scope = f"jobs.{owner.group(2)}"
                    break
        found[scope] = values
    return found


def snapshot(read: Reader, workflows: list[str]) -> dict:
    def load(relative: str) -> dict:
        text = read(relative)
        return json.loads(text) if text else {}

    risks, launcher, destination = (load("contracts/tool_risks.json"), load("config/local_tool.json"),
                                    load("contracts/github_issue_destination.json"))
    return {
        "tool_risk_classes": risks.get("tools", {}),
        "minimum_profile": risks.get("minimum_profile", {}),
        "approval_required_actions": risks.get("actions_requiring_approval_if_enabled", []),
        "launcher_approval": {key: launcher.get(key) for key in ("publisher", "name", "version", "sha256")},
        "external_destinations": ({destination["destination"]: {
            "operation": destination.get("operation"), "credential_scope": destination.get("credential_scope")}}
            if destination.get("destination") else {}),
        "limits": load("config/limits.json"),
        "workflow_token_permissions": {Path(name).name: workflow_permissions(read(name) or "")
                                       for name in workflows},
    }


def current(root: Path = ROOT) -> dict:
    return snapshot(file_reader(root), workflow_names(root))


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def flatten(value, prefix: str = "") -> dict:
    if isinstance(value, dict) and value:
        out = {}
        for key, item in value.items():
            out.update(flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return out
    return {prefix: value}


def changes(before: dict | None, after: dict) -> list[dict]:
    old, new = (flatten(before) if before else {}), flatten(after)
    return [{"path": key, "before": old.get(key), "after": new.get(key)}
            for key in sorted(set(old) | set(new)) if old.get(key) != new.get(key)]


def load_history(root: Path = ROOT) -> list[str]:
    path = root / HISTORY
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def entry(lines: list[str], snap: dict, **fields) -> str:
    previous = json.loads(lines[-1]) if lines else None
    return json.dumps({
        "sequence": len(lines) + 1, **fields,
        "changes": changes(previous and previous["snapshot"], snap),
        "snapshot_sha256": digest(snap), "snapshot": snap,
        "previous_sha256": hashlib.sha256(lines[-1].encode()).hexdigest() if lines else None,
    }, sort_keys=True, ensure_ascii=False)


def problems(root: Path = ROOT) -> list[str]:
    lines, found = load_history(root), []
    if not lines:
        return ["no permission history"]
    for number, line in enumerate(lines, 1):
        item = json.loads(line)
        expected_previous = hashlib.sha256(lines[number - 2].encode()).hexdigest() if number > 1 else None
        if item["sequence"] != number or item["previous_sha256"] != expected_previous:
            found.append(f"entry {number}: the chain was edited")
        if item["snapshot_sha256"] != digest(item["snapshot"]):
            found.append(f"entry {number}: snapshot does not match its hash")
        before = json.loads(lines[number - 2])["snapshot"] if number > 1 else None
        if item["changes"] != changes(before, item["snapshot"]) or not item["changes"]:
            found.append(f"entry {number}: changes do not match the snapshots")
    unrecorded = changes(json.loads(lines[-1])["snapshot"], current(root))
    if unrecorded:
        found.append("unrecorded permission change: " + ", ".join(change["path"] for change in unrecorded))
    return found


def record(root: Path, reason: str, approved_by: str) -> dict:
    if not reason.strip() or not approved_by.strip():
        raise ValueError("a permission change needs a reason and an approver")
    lines, snap = load_history(root), current(root)
    if lines and not changes(json.loads(lines[-1])["snapshot"], snap):
        raise ValueError("no permission change to record")
    line = entry(lines, snap, source="record", commit=None, reason=reason, approved_by=approved_by,
                 recorded_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    with (root / HISTORY).open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
    return json.loads(line)


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def backfill(root: Path = ROOT) -> list[str]:
    """One entry per commit that changed the permission snapshot, oldest first."""
    log = git(root, "log", "--reverse", "--format=%H%x09%cI%x09%s", "--", *SOURCES).splitlines()
    lines: list[str] = []
    for row in log:
        commit, when, subject = row.split("\t", 2)
        listing = git(root, "ls-tree", "--name-only", commit, ".github/workflows/").split()
        workflows = sorted(name for name in listing if name.endswith(".yml"))

        def read(relative: str, commit: str = commit) -> str | None:
            try:
                return git(root, "show", f"{commit}:{relative}")
            except subprocess.CalledProcessError:
                return None

        snap = snapshot(read, workflows)
        if lines and not changes(json.loads(lines[-1])["snapshot"], snap):
            continue
        # The first entry is the oldest state in the available (possibly shallow) history.
        lines.append(entry(lines, snap, source="git history", commit=commit, reason=subject,
                           approved_by=None, recorded_at=when))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("snapshot")
    sub.add_parser("check")
    recording = sub.add_parser("record")
    recording.add_argument("--reason", required=True)
    recording.add_argument("--approved-by", required=True)
    sub.add_parser("backfill")
    args = parser.parse_args(argv)
    if args.action == "snapshot":
        print(json.dumps(current(), indent=1, sort_keys=True))
    elif args.action == "check":
        found = problems()
        print(json.dumps({"entries": len(load_history()), "problems": found}, indent=1))
        return 1 if found else 0
    elif args.action == "record":
        try:
            print(json.dumps(record(ROOT, args.reason, args.approved_by)["changes"], indent=1))
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 1
    else:
        lines = backfill()
        (ROOT / HISTORY).write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        print(f"{len(lines)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
