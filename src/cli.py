"""Host commands: predict, evaluate, run, and status."""

import argparse
import json
import tomllib
from pathlib import Path
from data import (
    load_cases,
    normalize_evaluation_phases,
    prediction_level,
    prediction_prompt_templates,
    prediction_time_budget,
)
from runner import Runner, save


def merge_rows_by_id(old_rows, new_rows):
    by_id = {}
    order = []
    for row in list(old_rows or []) + list(new_rows or []):
        if not isinstance(row, dict):
            continue
        cid = row.get("id")
        if not cid:
            continue
        if cid not in by_id:
            order.append(cid)
        by_id[cid] = row
    return [by_id[i] for i in order]


def load_json_list(path):
    path = Path(path)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
    except Exception:
        return []
    return data if isinstance(data, list) else []


PRED_HARNESSES = ("claude-code", "codex", "cursor", "kimi-code", "gemini-cli", "deepseek-harness", "muse-code")
EVAL_HARNESSES = ("browser-use", "codex")
RESUME_KEYS = ("harness", "model", "effort", "accounts", "time_budget", "level", "prompt_templates")


def running_stages(command):
    if command == "run":
        return ("prediction", "evaluation")
    if command == "predict":
        return ("prediction",)
    return ("evaluation",)


def apply_overrides(cfg, stage, values):
    table = cfg.get(stage)
    if table is None:
        if any(value is not None for value in values.values()):
            raise ValueError(f"--{stage}-* requires [{stage}] in config")
        return
    for key, value in values.items():
        if value is not None:
            table[key] = value


def validate_stage(cfg, stage):
    if stage not in cfg:
        raise ValueError(f"missing [{stage}] in config")
    allowed = PRED_HARNESSES if stage == "prediction" else EVAL_HARNESSES
    harness = cfg[stage].get("harness")
    if harness not in allowed:
        if stage == "prediction":
            raise ValueError(
                "prediction harness must be claude-code, codex, cursor, kimi-code, gemini-cli, deepseek-harness, or muse-code"
            )
        raise ValueError("evaluation harness must be browser-use or codex")
    if stage == "prediction":
        minutes = prediction_time_budget(cfg)
        cfg[stage]["time_budget"] = "unlimited" if minutes is None else minutes
        if minutes is not None:
            if harness not in ("codex", "claude-code"):
                raise ValueError("Timed prediction supports codex and claude-code only")
            if minutes * 60 + 600 >= cfg[stage].get("timeout_seconds", 28800):
                raise ValueError("prediction.timeout_seconds must exceed time_budget * 60 + 600; allow startup time in addition to both five-minute grace periods")
    names = cfg[stage].get("accounts")
    if not names:
        raise ValueError(f"{stage}.accounts is required")
    accounts = cfg.get("accounts") or {}
    for name in names:
        if name not in accounts:
            raise ValueError(f"unknown account {name} for {stage}")
        if accounts[name].get("harness") != harness:
            raise ValueError(f"Account {name} has the wrong harness for {stage}")


def read_case_ids(path):
    ids = [value for line in Path(path).expanduser().read_text().splitlines()
           for value in line.split('#', 1)[0].split()]
    if not ids:
        raise ValueError("Case ID file is empty")
    if len(ids) != len(set(ids)):
        raise ValueError("Case ID file contains duplicates")
    return ids


def main(argv=None):
    p = argparse.ArgumentParser(prog="python scripts/run.py")
    p.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["predict", "evaluate", "run", "status"],
    )
    p.add_argument("--config", default="configs/config.toml")
    selection = p.add_mutually_exclusive_group()
    selection.add_argument("--cases", nargs="+")
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--cases-file", help="Task IDs separated by whitespace; # comments allowed")
    p.add_argument("--run-dir", default="runs/example")
    p.add_argument("--predictions", help="Prediction run directory for evaluate")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--prediction-harness", choices=PRED_HARNESSES)
    p.add_argument("--prediction-model")
    p.add_argument("--prediction-effort")
    p.add_argument("--prediction-accounts", nargs="+")
    p.add_argument("--evaluation-harness", choices=EVAL_HARNESSES)
    p.add_argument("--evaluation-model")
    p.add_argument("--evaluation-effort")
    p.add_argument("--evaluation-accounts", nargs="+")
    args = p.parse_args(argv)
    if args.cases_file:
        try:
            args.cases = read_case_ids(args.cases_file)
        except (OSError, ValueError) as exc:
            p.error(str(exc))
    root = Path(args.run_dir).expanduser().resolve()
    if args.command == "status":
        for stage in ["prediction", "evaluation"]:
            file = root / f"{stage}-summary.json"
            if file.exists():
                print(file.read_text())
        for path in sorted(root.glob("*/sweeper-*/result.json")):
            d = json.loads(path.read_text())
            print(path.parent.name, path.parent.parent.name, d.get("status"))
        return
    config_path = Path(args.config).resolve()
    if not config_path.is_file():
        raise SystemExit(
            f"{args.config} not found; copy configs/config.toml.example to "
            "configs/config.toml or pass --config"
        )
    cfg = tomllib.loads(config_path.read_text())
    repo = Path(__file__).resolve().parents[1]
    dataset = cfg.setdefault("dataset", {})
    dataset.setdefault("repo", "EVIGBYEN/SWEeper-Bench")
    dataset.setdefault("file", "data/test.jsonl")
    patches = Path(dataset.get("reference_patches") or "data/patches").expanduser()
    if not patches.is_absolute():
        patches = (repo / patches).resolve()
    dataset["reference_patches"] = str(patches)
    stages = running_stages(args.command)
    overrides = {
        "prediction": {
            "harness": args.prediction_harness,
            "model": args.prediction_model,
            "effort": args.prediction_effort,
            "accounts": args.prediction_accounts,
        },
        "evaluation": {
            "harness": args.evaluation_harness,
            "model": args.evaluation_model,
            "effort": args.evaluation_effort,
            "accounts": args.evaluation_accounts,
        },
    }
    for stage in stages:
        apply_overrides(cfg, stage, overrides[stage])
        validate_stage(cfg, stage)
    if "prediction" in stages:
        cfg["prediction"]["level"] = prediction_level(cfg)
        cfg["prediction"]["prompt_templates"] = prediction_prompt_templates(cfg)
    if "evaluation" in stages:
        cfg["evaluation"]["phases"] = normalize_evaluation_phases(cfg)
    predictions = Path(args.predictions).resolve() if args.predictions else root
    if not args.cases and not args.all:
        p.error("Choose --cases ID [ID ...], --cases-file PATH, or --all explicitly")
    from modal_vm import Backend, require_host_env

    require_host_env(cfg, stages)
    rows, data = load_cases(cfg, args.cases)
    if (
        root.exists()
        and (root / "config.json").exists()
        and not args.resume
        and args.command != "evaluate"
    ):
        p.error("Run directory already used. Choose a new --run-dir or --resume.")
    root.mkdir(parents=True, exist_ok=True)
    if args.resume and (root / "config.json").exists():
        old = json.loads((root / "config.json").read_text())
        for stage in stages:
            if stage not in old:
                continue
            for key in RESUME_KEYS:
                if old[stage].get(key) != cfg[stage].get(key):
                    raise ValueError(
                        "Resume cannot change model/harness/effort/accounts; use a new run"
                    )
    save(root / "config.json", cfg)
    cases = merge_rows_by_id(load_json_list(root / "cases.json"), rows)
    data["count"] = len(cases)
    save(root / "dataset.json", data)
    save(root / "cases.json", cases)
    from provenance import capture

    if not (root / "provenance.json").exists():
        save(root / "provenance.json", capture(repo, cfg, rows))
    backend = Backend(cfg)
    runner = Runner(cfg, backend, root)
    outcomes = []
    if args.command in ["predict", "run"]:
        outcomes.extend(runner.run("prediction", rows, args.resume))
    if args.command in ["evaluate", "run"]:
        outcomes.extend(runner.run("evaluation", rows, args.resume, predictions))
    return 1 if any(r.get("status") != "completed" for r in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
