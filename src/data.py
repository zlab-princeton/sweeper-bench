"""Load cases from Hugging Face. Host-only reference patches live under data/patches/."""

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path

import yaml
from huggingface_hub import HfApi, hf_hub_download

REQUIRED = {
    "case_id",
    "slug",
    "repo",
    "base_commit",
    "reference_commit",
    "task_scoped",
    "task_specified",
    "pr",
    "issue",
    "workflows",
}
PREDICTION_LEVELS = ("specified", "scoped")
PROMPT_TEMPLATES = ("browser", "non-browser")
EVAL_PHASE_CONCRETE = ("baseline", "reference", "prediction-specified", "prediction-scoped")


def patches_dir(config):
    raw = config.get("dataset", {}).get("reference_patches") or "data/patches"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    return path.resolve()


def patch_path(config, case_id):
    return patches_dir(config) / f"{case_id}.patch"


def validate_workflows(text, case_id):
    data = yaml.safe_load(text) or {}
    items = data.get("workflows") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != 2:
        raise ValueError(f"{case_id}: workflows must list target and preservation")
    cats = [str(item.get("category") or "") for item in items]
    if sorted(cats) != ["preservation", "target"]:
        raise ValueError(f"{case_id}: expected one target and one preservation workflow")
    if any(not item.get("steps") for item in items):
        raise ValueError(f"{case_id}: workflow steps required")
    return text


def adapt(raw):
    missing = REQUIRED - set(raw)
    unexpected = set(raw) - REQUIRED
    if missing or unexpected:
        raise ValueError(
            f"dataset schema mismatch: missing={sorted(missing)} unexpected={sorted(unexpected)}"
        )
    case_id = raw["case_id"]
    if not re.fullmatch(r"sweeper-\d{3}", case_id):
        raise ValueError("Invalid case id")
    if not re.fullmatch(r"[0-9a-f]{40}", raw["base_commit"]):
        raise ValueError("Invalid base_commit")
    scoped = str(raw.get("task_scoped") or "").strip()
    specified = str(raw.get("task_specified") or "").strip()
    if not scoped or not specified:
        raise ValueError(f"{case_id}: task_scoped and task_specified required")
    workflows = validate_workflows(raw["workflows"], case_id)
    return {
        "id": case_id,
        "slug": raw.get("slug") or "",
        "repo": raw["repo"],
        "base_commit": raw["base_commit"],
        "reference_commit": raw.get("reference_commit") or "",
        "pr": raw.get("pr"),
        "issue": raw.get("issue"),
        "task": {"specified": specified, "scoped": scoped},
        "workflows": workflows,
    }


def pred_row(row, level="scoped"):
    if level not in PREDICTION_LEVELS:
        level = "scoped"
    return {
        "id": row["id"],
        "repo": row["repo"],
        "base_commit": row["base_commit"],
        "task": {level: row["task"][level]},
    }


def eval_row(row):
    return {
        "id": row["id"],
        "repo": row["repo"],
        "base_commit": row["base_commit"],
        "reference_commit": row.get("reference_commit") or "",
        "workflows": row["workflows"],
    }


def load_cases(config, ids=None):
    dataset = config["dataset"]
    repo = dataset.get("repo") or "EVIGBYEN/SWEeper-Bench"
    name = dataset.get("file") or "data/test.jsonl"
    token = os.environ.get("HF_TOKEN") or True
    revision = HfApi().dataset_info(
        repo, revision=dataset.get("revision") or "main", token=token
    ).sha
    file = hf_hub_download(
        repo, name, repo_type="dataset", revision=revision, token=token
    )
    raw = Path(file).read_bytes()
    rows = [adapt(json.loads(line)) for line in raw.splitlines() if line.strip()]
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate cases")
    if ids:
        missing = set(ids) - {r["id"] for r in rows}
        if missing:
            raise ValueError(f"Unknown cases: {sorted(missing)}")
        rows = [r for r in rows if r["id"] in ids]
    return rows, {
        "repo": repo,
        "file": name,
        "revision": revision,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "count": len(rows),
        "reference_patches": str(patches_dir(config)),
    }


def write_workflow(row, dest):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(row["workflows"], encoding="utf-8")
    return dest


def prediction_time_budget(config):
    """None means unlimited. A positive number is minutes."""
    pred = config.get("prediction")
    if not isinstance(pred, dict):
        pred = {}
    raw = pred.get("time_budget", "unlimited")
    if isinstance(raw, str) and raw.strip().lower() == "unlimited":
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or raw <= 0:
        raise ValueError('prediction.time_budget must be "unlimited" or a positive number of minutes')
    return raw


def prediction_level(config):
    pred = config.get("prediction")
    if not isinstance(pred, dict):
        pred = {}
    level = str(pred.get("level", "scoped")).strip().lower()
    if level not in PREDICTION_LEVELS:
        raise ValueError(f"prediction.level must be one of {PREDICTION_LEVELS}")
    return level


def prediction_prompt_templates(config):
    pred = config.get("prediction")
    if not isinstance(pred, dict):
        pred = {}
    name = str(pred.get("prompt_templates", "non-browser")).strip().lower()
    if name not in PROMPT_TEMPLATES:
        raise ValueError(f"prediction.prompt_templates must be one of {PROMPT_TEMPLATES}")
    return name


def normalize_evaluation_phases(config):
    raw = config.get("evaluation", {}).get("phases", ["prediction"])
    if raw is None:
        raw = ["prediction"]
    if isinstance(raw, str):
        raw = [item for item in re.split(r"[\s,]+", raw.strip()) if item]
    out = []
    seen = set()
    for item in raw:
        phase = str(item).strip().lower()
        if phase == "prediction":
            phase = f"prediction-{prediction_level(config)}"
        if phase not in EVAL_PHASE_CONCRETE:
            raise ValueError(
                "evaluation.phases must be baseline, reference, and/or prediction "
                f"(got {item!r})"
            )
        if phase not in seen:
            seen.add(phase)
            out.append(phase)
    if not out:
        raise ValueError("evaluation.phases must list at least one of baseline, reference, prediction")
    return out


def needs_prediction_phase(config):
    return any(phase.startswith("prediction-") for phase in normalize_evaluation_phases(config))


def runnable_evaluation_phases(config, case_id, incoming=None):
    incoming = incoming or {}
    phases = []
    for phase in normalize_evaluation_phases(config):
        if phase == "baseline":
            phases.append(phase)
        elif phase == "reference" and (
            incoming.get("reference") or patch_path(config, case_id).is_file()
        ):
            phases.append(phase)
        elif phase.startswith("prediction-") and incoming.get(phase):
            phases.append(phase)
    return phases


def evaluation_cells_complete(config, row, artifacts, incoming=None):
    from runtime.parse_results import task_is_complete

    phases = runnable_evaluation_phases(config, row["id"], incoming)
    artifacts = Path(artifacts)
    if not phases or not (artifacts / "result.jsonl").is_file():
        return False
    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    workflows = Path(handle.name)
    try:
        handle.write(row["workflows"])
        handle.close()
        return task_is_complete(
            phases,
            artifacts / "logs",
            artifacts / "result.jsonl",
            workflows,
            config.get("evaluation", {}).get("categories", "target,preservation"),
            "",
        )
    except Exception:
        # Completeness check bugs must not look like a VM/worker failure.
        return False
    finally:
        workflows.unlink(missing_ok=True)


def prediction_patch_complete(config, artifacts):
    path = Path(artifacts) / f"prediction-{prediction_level(config)}.patch"
    if not path.is_file():
        return False
    return "diff --git " in path.read_text(errors="replace")
