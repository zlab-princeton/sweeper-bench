#!/usr/bin/env python3
"""Aggregate evaluation cells, check completeness, and clear a phase for retry."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


CATEGORIES = ("target", "preservation")
VALID_RESULTS = {"pass", "fail", "uncertain"}
CELL_FIELDS = ("pass", "fail", "uncertain", "fail_id", "uncertain_id", "workflows")
WORKFLOW_FIELDS = (
    "id",
    "category",
    "purpose",
    "result",
    "agent_result",
    "agent_text",
    "attempts",
    "llm_audit",
)
JSONL_KEYS = ("phase", "category", "pass", "fail", "uncertain")
CATEGORY_ALIASES = {
    "target": "target",
    "preservation": "preservation",
}


def split_csv(raw: str) -> list[str]:
    return [item for item in re.split(r"[\s,]+", (raw or "").strip()) if item]


def parse_category_list(raw: str) -> list[str]:
    """Parse a target/preservation filter. Empty means no extra filter."""
    if not (raw or "").strip():
        return []
    categories: list[str] = []
    for value in split_csv(raw.lower()):
        category = CATEGORY_ALIASES.get(value)
        if not category:
            allowed = ", ".join(CATEGORIES)
            raise SystemExit(f"unknown category: {value}; allowed: {allowed}")
        if category not in categories:
            categories.append(category)
    return categories


def parse_id_list(raw: str) -> set[str]:
    return {item for item in split_csv(raw) if item}


def is_nonneg_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def load_json_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_workflow_specs(workflows_path: Path) -> list[dict[str, str]]:
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit("completeness check requires PyYAML") from exc

    workflows_path = Path(workflows_path)
    if not workflows_path.is_file():
        raise SystemExit(f"workflows not found: {workflows_path}")
    data = yaml.safe_load(workflows_path.read_text(encoding="utf-8")) or {}
    specs: list[dict[str, str]] = []
    for item in data.get("workflows") or []:
        if not isinstance(item, dict):
            continue
        workflow_id = item.get("id")
        category = item.get("category")
        if not workflow_id or category not in CATEGORIES:
            continue
        specs.append({"id": str(workflow_id), "category": category})
    if not specs:
        raise SystemExit(f"no workflows in {workflows_path}")
    return specs


def intended_specs(
    workflows_path: Path,
    categories_raw: str = "",
    workflow_ids_raw: str = "",
) -> list[dict[str, str]]:
    allowed_categories = set(parse_category_list(categories_raw))
    allowed_ids = parse_id_list(workflow_ids_raw)
    specs: list[dict[str, str]] = []
    for spec in load_workflow_specs(workflows_path):
        if allowed_categories and spec["category"] not in allowed_categories:
            continue
        if allowed_ids and spec["id"] not in allowed_ids:
            continue
        specs.append(spec)
    return specs


def intended_categories(
    workflows_path: Path,
    categories_raw: str = "",
    workflow_ids_raw: str = "",
) -> list[str]:
    categories: list[str] = []
    for spec in intended_specs(workflows_path, categories_raw, workflow_ids_raw):
        if spec["category"] not in categories:
            categories.append(spec["category"])
    return categories


def expected_ids(
    workflows_path: Path,
    category: str,
    workflow_ids_raw: str = "",
    categories_raw: str = "",
) -> list[str]:
    return [
        spec["id"]
        for spec in intended_specs(workflows_path, categories_raw, workflow_ids_raw)
        if spec["category"] == category
    ]


def normalize_llm_audit(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"verdict": None, "failure_reason": "not judged"}
    verdict = value.get("verdict")
    if verdict is not None and not isinstance(verdict, bool):
        verdict = None
    reason = value.get("failure_reason")
    if reason is None:
        reason = "not judged" if verdict is None else ""
    else:
        reason = str(reason)
    if verdict is None and not reason:
        reason = "not judged"
    return {"verdict": verdict, "failure_reason": reason}


def is_llm_audit(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if set(value.keys()) != {"verdict", "failure_reason"}:
        return False
    verdict = value["verdict"]
    if verdict is not None and not isinstance(verdict, bool):
        return False
    return isinstance(value["failure_reason"], str)


def summarize_cell(category: str, workflow_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize one cell."""
    if category not in CATEGORIES:
        raise ValueError(f"unknown category: {category}")

    cell: dict[str, Any] = {
        "pass": 0,
        "fail": 0,
        "uncertain": 0,
        "fail_id": [],
        "uncertain_id": [],
        "workflows": [],
    }
    for item in workflow_results:
        if item.get("category") != category:
            continue
        result = item.get("result")
        workflow_id = str(item.get("id") or "unknown")
        if result == "pass":
            cell["pass"] += 1
        elif result == "uncertain":
            cell["uncertain"] += 1
            cell["uncertain_id"].append(workflow_id)
        else:
            cell["fail"] += 1
            cell["fail_id"].append(workflow_id)
        stored = dict(item)
        stored["llm_audit"] = normalize_llm_audit(stored.get("llm_audit"))
        cell["workflows"].append(stored)
    return cell


def workflow_item_complete(item: Any, category: str) -> bool:
    if not isinstance(item, dict):
        return False
    for field in WORKFLOW_FIELDS:
        if field not in item:
            return False
    if not isinstance(item["id"], str) or not item["id"].strip():
        return False
    if item["category"] != category:
        return False
    if not isinstance(item["purpose"], str):
        return False
    if item["result"] not in VALID_RESULTS:
        return False
    if not isinstance(item["agent_result"], dict):
        return False
    if not isinstance(item["agent_text"], str):
        return False
    if not is_positive_int(item["attempts"]):
        return False
    if not is_llm_audit(item["llm_audit"]):
        return False
    return True


def cell_object_complete(
    cell: Any,
    category: str,
    expected_workflow_ids: list[str] | None = None,
) -> bool:
    if not isinstance(cell, dict):
        return False
    for field in CELL_FIELDS:
        if field not in cell:
            return False
    if not is_nonneg_int(cell["pass"]) or not is_nonneg_int(cell["fail"]) or not is_nonneg_int(cell["uncertain"]):
        return False
    if not isinstance(cell["fail_id"], list) or not isinstance(cell["uncertain_id"], list):
        return False
    if not isinstance(cell["workflows"], list) or not cell["workflows"]:
        return False
    if not all(isinstance(value, str) and value for value in cell["fail_id"]):
        return False
    if not all(isinstance(value, str) and value for value in cell["uncertain_id"]):
        return False

    workflows = cell["workflows"]
    if not all(workflow_item_complete(item, category) for item in workflows):
        return False

    ids = [item["id"] for item in workflows]
    if len(ids) != len(set(ids)):
        return False
    if expected_workflow_ids is not None:
        if ids != expected_workflow_ids:
            return False
    if not ids:
        return False

    pass_ids = [item["id"] for item in workflows if item["result"] == "pass"]
    fail_ids = [item["id"] for item in workflows if item["result"] == "fail"]
    uncertain_ids = [item["id"] for item in workflows if item["result"] == "uncertain"]
    if cell["pass"] != len(pass_ids):
        return False
    if cell["fail"] != len(fail_ids) or cell["fail_id"] != fail_ids:
        return False
    if cell["uncertain"] != len(uncertain_ids) or cell["uncertain_id"] != uncertain_ids:
        return False
    return True


def is_jsonl_record(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    if set(obj.keys()) != set(JSONL_KEYS):
        return False
    if not isinstance(obj["phase"], str) or not obj["phase"].strip():
        return False
    if obj["category"] not in CATEGORIES:
        return False
    return all(is_nonneg_int(obj[key]) for key in ("pass", "fail", "uncertain"))


def jsonl_cell_counts(
    jsonl_path: Path,
    phase: str,
    category: str,
) -> tuple[int, int, int] | None:
    """Each (phase, category) has exactly one row."""
    if not jsonl_path.is_file():
        return None
    matches: list[dict[str, Any]] = []
    try:
        raw_lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not is_jsonl_record(obj):
            continue
        if obj["phase"] == phase and obj["category"] == category:
            matches.append(obj)
    if len(matches) != 1:
        return None
    record = matches[0]
    return int(record["pass"]), int(record["fail"]), int(record["uncertain"])


def is_cell_complete(
    result_json_path: Path,
    jsonl_path: Path,
    phase: str,
    category: str,
    expected_workflow_ids: list[str],
) -> bool:
    if category not in CATEGORIES or not expected_workflow_ids:
        return False
    data = load_json_object(result_json_path)
    if data is None:
        return False
    if data.get("phase") != phase:
        return False
    cell = data.get(category)
    if not cell_object_complete(cell, category, expected_workflow_ids):
        return False
    counts = jsonl_cell_counts(jsonl_path, phase, category)
    if counts is None:
        return False
    return counts == (cell["pass"], cell["fail"], cell["uncertain"])


def incomplete_categories(
    result_json_path: Path,
    jsonl_path: Path,
    phase: str,
    workflows_path: Path,
    categories_raw: str = "",
    workflow_ids_raw: str = "",
) -> list[str]:
    """If any cell in a category is incomplete, return every cell in that category."""
    intended = intended_categories(workflows_path, categories_raw, workflow_ids_raw)
    if not intended:
        raise SystemExit(
            f"incomplete-categories: no categories to check ({workflows_path})"
        )
    for category in intended:
        ids = expected_ids(workflows_path, category, workflow_ids_raw, categories_raw)
        if not is_cell_complete(result_json_path, jsonl_path, phase, category, ids):
            return list(intended)
    return []


def task_is_complete(
    phases: list[str],
    logs_dir: Path,
    jsonl_path: Path,
    workflows_path: Path,
    categories_raw: str = "",
    workflow_ids_raw: str = "",
) -> bool:
    if not phases:
        return False
    logs_dir = Path(logs_dir)
    jsonl_path = Path(jsonl_path)
    workflows_path = Path(workflows_path)
    for phase in phases:
        result_json = logs_dir / f"{phase}-result.json"
        if incomplete_categories(
            result_json,
            jsonl_path,
            phase,
            workflows_path,
            categories_raw,
            workflow_ids_raw,
        ):
            return False
    return True


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def merge_write_result_json(
    result_json_path: Path,
    phase: str,
    category: str,
    cell: dict[str, Any],
) -> None:
    existing = load_json_object(result_json_path) or {}
    out: dict[str, Any] = {"phase": phase}
    for item_category in CATEGORIES:
        if item_category == category:
            out[item_category] = cell
            continue
        sibling = existing.get(item_category)
        if cell_object_complete(sibling, item_category):
            out[item_category] = sibling
    atomic_write_text(result_json_path, json.dumps(out, indent=2, ensure_ascii=False) + "\n")


def upsert_result_jsonl(
    jsonl_path: Path,
    phase: str,
    category: str,
    pass_count: int,
    fail_count: int,
    uncertain_count: int,
) -> None:
    record = {
        "phase": phase,
        "category": category,
        "pass": int(pass_count),
        "fail": int(fail_count),
        "uncertain": int(uncertain_count),
    }
    new_line = json.dumps(record, ensure_ascii=False)
    kept: list[str] = []
    replaced = False
    if jsonl_path.is_file():
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not is_jsonl_record(obj):
                continue
            if obj["phase"] == phase and obj["category"] == category:
                if not replaced:
                    kept.append(new_line)
                    replaced = True
                continue
            kept.append(json.dumps(obj, ensure_ascii=False))
    if not replaced:
        kept.append(new_line)
    atomic_write_text(jsonl_path, ("\n".join(kept) + "\n") if kept else "")


def write_cell_results(
    result_json_path: Path,
    jsonl_path: Path,
    phase: str,
    category: str,
    workflow_results: list[dict[str, Any]],
) -> dict[str, Any]:
    cell = summarize_cell(category, workflow_results)
    merge_write_result_json(result_json_path, phase, category, cell)
    upsert_result_jsonl(
        jsonl_path,
        phase,
        category,
        cell["pass"],
        cell["fail"],
        cell["uncertain"],
    )
    return cell


def normalize_verdict_result(value: Any) -> str | None:
    if isinstance(value, str):
        result = value.strip().lower()
        if result in VALID_RESULTS:
            return result
    return None


def hard_uncertain(item: dict[str, Any]) -> bool:
    """Infrastructure or invalid JSON: retry that workflow without resetting the product."""
    agent_result = item.get("agent_result")
    if not isinstance(agent_result, dict):
        return True
    if agent_result.get("parse_error") or agent_result.get("error"):
        return True
    return normalize_verdict_result(agent_result.get("result")) is None


def agent_uncertain(item: dict[str, Any]) -> bool:
    """The agent reported uncertain, and the result is well formed."""
    return item.get("result") == "uncertain" and not hard_uncertain(item)


def resolve_categories(raw: str) -> list[str]:
    parsed = parse_category_list(raw)
    return parsed or list(CATEGORIES)


def agent_uncertain_ids(
    result_json_path: Path,
    phase: str,
    categories_raw: str = "",
) -> list[str]:
    """Workflow ids in this phase whose agent result is uncertain, in YAML order."""
    data = load_json_object(result_json_path)
    if data is None or data.get("phase") != phase:
        return []
    ids: list[str] = []
    for category in resolve_categories(categories_raw):
        cell = data.get(category)
        if not isinstance(cell, dict):
            continue
        workflows = cell.get("workflows")
        if not isinstance(workflows, list):
            continue
        for item in workflows:
            if isinstance(item, dict) and agent_uncertain(item):
                workflow_id = item.get("id")
                if isinstance(workflow_id, str) and workflow_id.strip():
                    ids.append(workflow_id)
    return ids


def clear_phase_cells(
    result_json_path: Path,
    jsonl_path: Path,
    phase: str,
    categories_raw: str = "",
) -> list[str]:
    """Delete saved cells for this phase so the phase can be rerun."""
    categories = resolve_categories(categories_raw)
    existing = load_json_object(result_json_path) or {}
    out: dict[str, Any] = {"phase": phase}
    for item_category in CATEGORIES:
        if item_category in categories:
            continue
        sibling = existing.get(item_category)
        if sibling is not None:
            out[item_category] = sibling
    atomic_write_text(result_json_path, json.dumps(out, indent=2, ensure_ascii=False) + "\n")

    kept: list[str] = []
    if jsonl_path.is_file():
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not is_jsonl_record(obj):
                continue
            if obj["phase"] == phase and obj["category"] in categories:
                continue
            kept.append(json.dumps(obj, ensure_ascii=False))
    atomic_write_text(jsonl_path, ("\n".join(kept) + "\n") if kept else "")
    return categories


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workflows", required=True)
    parser.add_argument("--categories", default="")
    parser.add_argument("--workflow-ids", default="")


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate evaluation cells and check completeness")
    sub = parser.add_subparsers(dest="command", required=True)

    incomplete = sub.add_parser("incomplete-categories", help="Print every cell in a category that has a gap")
    incomplete.add_argument("--phase", required=True)
    incomplete.add_argument("--result-json", required=True)
    incomplete.add_argument("--result-jsonl", required=True)
    _add_common_args(incomplete)

    complete = sub.add_parser("task-complete", help="Exit 0 when every configured cell is complete")
    complete.add_argument("--phases", required=True)
    complete.add_argument("--logs-dir", required=True)
    complete.add_argument("--result-jsonl", required=True)
    _add_common_args(complete)

    agent_cmd = sub.add_parser("agent-uncertain", help="Print workflow ids the agent marked uncertain")
    agent_cmd.add_argument("--phase", required=True)
    agent_cmd.add_argument("--result-json", required=True)
    agent_cmd.add_argument("--categories", default="")

    clear_cmd = sub.add_parser("clear-phase", help="Delete saved cells for one phase")
    clear_cmd.add_argument("--phase", required=True)
    clear_cmd.add_argument("--result-json", required=True)
    clear_cmd.add_argument("--result-jsonl", required=True)
    clear_cmd.add_argument("--categories", default="")

    args = parser.parse_args()
    if args.command == "agent-uncertain":
        ids = agent_uncertain_ids(Path(args.result_json), args.phase, args.categories)
        print(" ".join(ids))
        return
    if args.command == "clear-phase":
        clear_phase_cells(
            Path(args.result_json),
            Path(args.result_jsonl),
            args.phase,
            args.categories,
        )
        return
    workflows_path = Path(args.workflows)
    if args.command == "incomplete-categories":
        missing = incomplete_categories(
            Path(args.result_json),
            Path(args.result_jsonl),
            args.phase,
            workflows_path,
            args.categories,
            args.workflow_ids,
        )
        print(" ".join(missing))
        return
    if args.command == "task-complete":
        ok = task_is_complete(
            split_csv(args.phases),
            Path(args.logs_dir),
            Path(args.result_jsonl),
            workflows_path,
            args.categories,
            args.workflow_ids,
        )
        raise SystemExit(0 if ok else 1)
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(f"parse_results crashed: {exc}") from exc
