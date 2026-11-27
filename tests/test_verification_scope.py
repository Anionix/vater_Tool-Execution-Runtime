"""task.testing.021: executed and unexecuted scope are recorded separately.

docs/verification_scope.json covers the three areas Python tests cannot
settle alone: external services, official MCP and durable records. These
tests keep the record honest:

* an executed item points only at evidence that really ran and passed;
* a not-executed item claims no evidence, says why, and is shown as
  unconfirmed; its independent tests (local substitutes) must exist;
* a not-executed item that names a backlog task blocks that task from being
  marked done, so the ledger cannot claim what the record says was not run.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCOPE = json.loads((ROOT / "docs" / "verification_scope.json").read_text(encoding="utf-8"))
ITEMS = [(area, item) for area, items in SCOPE["areas"].items() for item in items]
TASKS = {row["task_id"]: row for row in map(json.loads, (ROOT / "backlog" / "tasks.jsonl").read_text().splitlines())}


def item_id(value):
    return value["id"] if isinstance(value, dict) else value


def defines(node: str) -> bool:
    file, _, name = node.partition("::")
    tree = ast.parse((ROOT / file).read_text(encoding="utf-8"))
    return name in {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_the_three_areas_each_have_executed_and_unconfirmed_items():
    assert set(SCOPE["areas"]) == {"external_services", "official_mcp", "durable_records"}
    for area, items in SCOPE["areas"].items():
        assert {item["status"] for item in items} == {"executed", "not_executed"}, area
    ids = [item["id"] for _, item in ITEMS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("area,item", ITEMS, ids=item_id)
def test_item_claims_only_what_ran(area, item):
    assert item["scope"] and item["status"] in {"executed", "not_executed"}
    for node in item["independent_tests"]:
        assert defines(node), node
    if item["status"] == "executed":
        assert item["evidence"], "an executed item must point at its evidence"
        for relative in item["evidence"]:
            path = (ROOT / relative).resolve()
            assert path.is_relative_to(ROOT / "evidence") and path.is_file(), relative
            record = json.loads(path.read_text(encoding="utf-8"))
            if "report" in record and "provenance" in record:
                assert record["report"]["all_passed"] is True, relative  # a live run
            else:
                assert record.get("executed") is True and record.get("verification_status") == "passed", relative
    else:
        # Unconfirmed: no evidence is claimed and the gap is explained.
        assert item["evidence"] == [] and item["reason"]


@pytest.mark.parametrize("area,item", [(a, i) for a, i in ITEMS if i["status"] == "not_executed"], ids=item_id)
def test_an_unexecuted_item_keeps_its_task_open(area, item):
    for task_id in re.findall(r"task\.[a-z_]+\.\d{3}", item["reason"]):
        assert TASKS[task_id]["status"] != "done", (
            f"{item['id']} says {task_id} was not run; update docs/verification_scope.json before closing it")
