#!/usr/bin/env python3
"""Run each workflow in evaluation.workflows.yaml."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from playwright.sync_api import sync_playwright


RESULT_JSON_STEP = """When finished, reply with ONLY valid JSON (no markdown fences) using this schema:
{
  "result": "pass" or "fail" or "uncertain",
  "substeps": [
    {"step": "short step label", "result": "pass" or "fail" or "uncertain", "reason": "what you observed"}
  ]
}"""


def load_workflows(path: Path) -> list[dict[str, Any]]:
    """Load workflows from the task YAML."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    workflows = data.get("workflows") or []
    if not workflows:
        raise SystemExit(f"no workflows in {path}")
    for workflow in workflows:
        steps = list(workflow.get("steps") or [])
        if steps[-1:] != [RESULT_JSON_STEP]:
            steps.append(RESULT_JSON_STEP)
        workflow["steps"] = steps
    return workflows


def configure_eval_logging() -> None:
    """Quiet browser-use and Playwright step logs, which include the full task."""
    if os.environ.get("AGENT_BROWSER_VERBOSE", "").strip().lower() in ("1", "true", "yes"):
        return
    for name in ("browser_use", "playwright", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def apply_setup_sync(base_url: str, setup: list[dict[str, Any]], state_path: Path) -> None:
    """Apply setup with the sync Playwright API, off the asyncio thread."""
    if not setup:
        state_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        return

    setup_navigation_timeout_ms = 120_000
    chromium_path = os.environ.get("CHROME_PATH", "/usr/bin/chromium")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=chromium_path if Path(chromium_path).exists() else None,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context()
        page = context.new_page()
        page.goto(base_url, wait_until="domcontentloaded", timeout=setup_navigation_timeout_ms)

        for item in setup:
            kind = item.get("type")
            if kind == "clear_localStorage":
                page.evaluate("(k) => localStorage.removeItem(k)", item["key"])
            elif kind == "set_localStorage":
                page.evaluate(
                    "({ key, value }) => localStorage.setItem(key, JSON.stringify(value))",
                    {"key": item["key"], "value": item["value"]},
                )
            else:
                raise ValueError(f"unknown setup type: {kind}")

        context.storage_state(path=str(state_path))
        browser.close()


async def apply_setup(base_url: str, setup: list[dict[str, Any]], state_path: Path) -> None:
    await asyncio.to_thread(apply_setup_sync, base_url, setup, state_path)


def build_task(base_url: str, workflow: dict[str, Any]) -> str:
    """Build the verifier prompt for one workflow."""
    steps = workflow.get("steps") or []
    numbered = "\n".join(f"{index + 1}. {step}" for index, step in enumerate(steps))
    return f"""You are an evaluation agent testing a web application.

Application URL: {base_url}
Workflow id: {workflow.get('id', 'unknown')}
Purpose: {workflow.get('purpose', '')}

Execute the steps below in order. Start by navigating to {base_url}.

Steps:
{numbered}

When finished, reply with ONLY valid JSON (no markdown fences) using this schema:
{{
  "result": "pass" or "fail" or "uncertain",
  "substeps": [
    {{"step": "short step label", "result": "pass" or "fail" or "uncertain", "reason": "what you observed"}}
  ]
}}

Rules:
- Use "pass" only if every verification step succeeded.
- Use "fail" only if the app loaded and you clearly observed product behavior that violates the workflow.
- Do NOT use "uncertain" lightly. If the page is slow, wait; if navigation fails, retry normal reload/navigation; if an interaction is flaky, retry the intended UI interaction without changing the workflow semantics.
- Use "uncertain" only when the evaluation did not reliably reach or observe the product after reasonable wait/retry because of infrastructure, loading, browser/tooling, or network problems.
- Examples that may be "uncertain": product frontend unreachable, connection refused, 502/Bad Gateway, net::ERR_* browser error page, page never leaves loading/blank state, browser/CDP/Playwright failure, tool interruption, invalid/empty final JSON, or inability to establish the workflow precondition due to the app not loading.
- Do not mark a real product bug as "uncertain" just because the workflow failed. If the product UI is reachable and you can observe concrete behavior, choose "pass" or "fail".
- If you cannot identify the correct element after reasonable attempts because the page is loaded but ambiguous, report exactly what blocked reliable observation and use "uncertain" only if you cannot make a trustworthy pass/fail judgment.
- Report what you actually see in the UI; do not guess.
- If a button or link must be absent, explicitly confirm you cannot find it.
"""


def _strip_browser_use_attachments(text: str) -> str:
    """Drop the attachment block browser-use appends after done()."""
    marker = "\n\nAttachments:\n"
    index = text.find(marker)
    if index == -1:
        return text
    return text[:index]


def _strip_trailing_commas(text: str) -> str:
    """Remove trailing commas outside strings."""
    out: list[str] = []
    in_str = False
    escape = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


_FENCED_JSON = re.compile(r"```(?:json|JSON)?\s*\r?\n(.*?)```", re.DOTALL)
_VERDICT_RESULTS = frozenset({"pass", "fail", "uncertain"})


def _loads_json_object(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start != -1:
        try:
            parsed, _ = json.JSONDecoder().raw_decode(text, start)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


def _iter_json_objects(text: str) -> list[dict[str, Any]]:
    """Collect every JSON object that raw_decode can parse."""
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    index = 0
    while True:
        start = text.find("{", index)
        if start == -1:
            break
        try:
            parsed, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(parsed, dict):
            found.append(parsed)
        index = end
    return found


def _candidate_texts(text: str) -> list[str]:
    bodies = [m.group(1).strip() for m in _FENCED_JSON.finditer(text) if m.group(1).strip()]
    bodies.append(text)
    return bodies


def _has_verdict(obj: dict[str, Any]) -> bool:
    value = obj.get("result")
    return isinstance(value, str) and value.strip().lower() in _VERDICT_RESULTS


def _pick_verdict_object(objects: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer an object with a valid result. Ties keep the last one."""
    if not objects:
        return None
    with_verdict = [obj for obj in objects if _has_verdict(obj)]
    return (with_verdict or objects)[-1]


def extract_json_blob(text: str) -> dict[str, Any]:
    """Parse the verdict JSON from the agent reply."""
    original = (text or "").strip()
    if not original:
        return {"result": "uncertain", "substeps": [], "parse_error": "empty agent reply"}

    text = _strip_browser_use_attachments(original).strip()
    objects: list[dict[str, Any]] = []
    for chunk in _candidate_texts(text):
        for raw in (chunk, _strip_trailing_commas(chunk)):
            objects.extend(_iter_json_objects(raw))
    picked = _pick_verdict_object(objects)
    if picked is not None:
        return picked

    return {"result": "uncertain", "substeps": [], "parse_error": "JSON parse failed", "raw": original}


CATEGORY_ABBR: dict[str, str] = {
    "target": "target",
    "preservation": "preservation",
}

CATEGORY_ALIASES: dict[str, str] = {
    "target": "target",
    "preservation": "preservation",
}


def parse_category_filter(raw: str) -> set[str]:
    """Parse a target/preservation filter. Commas or whitespace separate values."""
    values = [item for item in re.split(r"[\s,]+", raw.strip().lower()) if item]
    categories: set[str] = set()
    for value in values:
        category = CATEGORY_ALIASES.get(value)
        if not category:
            allowed = ", ".join(sorted(CATEGORY_ALIASES))
            raise SystemExit(f"unknown AGENT_BROWSER_WORKFLOW_CATEGORIES item: {value}; allowed: {allowed}")
        categories.add(category)
    return categories


def workflow_counters(
    workflow_id: str,
    all_workflows: list[dict[str, Any]],
) -> str:
    """Return this workflow's index and the task total, such as 2/9."""
    total_all = len(all_workflows)

    overall_idx = next(
        index for index, item in enumerate(all_workflows, start=1) if item.get("id") == workflow_id
    )
    return f"{overall_idx}/{total_all}"


def format_workflow_header(
    phase: str,
    workflow: dict[str, Any],
    all_workflows: list[dict[str, Any]],
) -> str:
    """Header line, for example WORKFLOWS baseline-target ( 2 / 9 ): id."""
    category = workflow.get("category") or "unknown"
    abbr = CATEGORY_ABBR.get(category, category)
    counters = workflow_counters(workflow["id"], all_workflows)
    current, total = counters.split("/", 1)
    return f"=== WORKFLOWS {phase}-{abbr} ( {current} / {total} ): {workflow['id']} ==="


DEFAULT_VIEWPORT = {"width": 1920, "height": 1200}


def workflow_viewport(workflow: dict[str, Any]) -> dict[str, int]:
    """Read an optional viewport. Default is 1920x1200."""
    viewport = workflow.get("viewport")
    if not viewport:
        return DEFAULT_VIEWPORT.copy()
    try:
        width = int(viewport["width"])
        height = int(viewport["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"workflow {workflow.get('id')} has an invalid viewport: {viewport}") from exc
    if width <= 0 or height <= 0:
        raise ValueError(f"workflow {workflow.get('id')} has an invalid viewport: {viewport}")
    return {"width": width, "height": height}


VALID_RESULTS = {"pass", "fail", "uncertain"}
MISSING_LLM_AUDIT = {"verdict": None, "failure_reason": "not judged"}
KNOWN_HARNESSES = ("browser-use", "codex")


def evaluation_harness_name() -> str:
    raw = os.environ.get("EVALUATION_HARNESS", "browser-use").strip().lower().replace("_", "-")
    if raw not in KNOWN_HARNESSES:
        raise SystemExit(f"unknown EVALUATION_HARNESS: {raw}; allowed: {', '.join(KNOWN_HARNESSES)}")
    return raw


def load_harness(name: str):
    path = Path(__file__).resolve().parent / "harnesses" / f"{name}.py"
    if not path.is_file():
        raise SystemExit(f"evaluation harness not found: {path}")
    spec = importlib.util.spec_from_file_location(f"eval_harness_{name.replace('-', '_')}", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load evaluation harness: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "build_agent"):
        raise SystemExit(f"harness {name} has no build_agent()")
    return module


def extract_llm_audit(history: Any) -> dict[str, Any]:
    """Copy the two judge fields for this workflow. They are not the verdict."""
    if history is None:
        return dict(MISSING_LLM_AUDIT)
    data = None
    judgement = getattr(history, "judgement", None)
    try:
        if callable(judgement):
            data = judgement()
        else:
            data = judgement
    except Exception:
        data = None
    if not isinstance(data, dict):
        return dict(MISSING_LLM_AUDIT)
    verdict = data.get("verdict")
    if not isinstance(verdict, bool):
        verdict = None
    reason = data.get("failure_reason")
    if reason is None:
        reason = ""
    else:
        reason = str(reason)
    if verdict is None:
        return {"verdict": None, "failure_reason": reason or "not judged"}
    return {"verdict": verdict, "failure_reason": reason}


def normalize_result(value: Any) -> str | None:
    if isinstance(value, str):
        result = value.strip().lower()
        if result in VALID_RESULTS:
            return result
    return None


def workflow_result(verdict: dict[str, Any]) -> str:
    """Accept only a top-level result. Invalid JSON is a hard_uncertain retry."""
    top_level = normalize_result(verdict.get("result"))
    if top_level:
        return top_level
    return "uncertain"


def recording_enabled() -> bool:
    """Record unless AGENT_BROWSER_RECORD is 0, false, or no."""
    return os.environ.get("AGENT_BROWSER_RECORD", "1").strip().lower() in ("1", "true", "yes")


def collect_recording_files(record_dir: Path) -> list[str]:
    """List video files in the recording directory."""
    if not record_dir.is_dir():
        return []
    names: list[str] = []
    for pattern in ("*.mp4", "*.webm", "*.gif"):
        names.extend(sorted(p.name for p in record_dir.glob(pattern)))
    return names


def safe_filename_component(value: str) -> str:
    """Turn a workflow or category label into a filename fragment."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = cleaned.strip(".-")
    return cleaned or "workflow"


def video_files(record_dir: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in ("*.mp4", "*.webm", "*.gif"):
        files.extend(sorted(record_dir.glob(pattern)))
    return files


def rename_recording_files(
    record_dir: Path,
    before_files: set[Path],
    workflow: dict[str, Any],
    phase: str,
) -> list[str]:
    """Rename generated videos to phase-workflow_id."""
    phase_name = safe_filename_component(phase)
    workflow_id = safe_filename_component(str(workflow.get("id") or "unknown"))
    stem = f"{phase_name}-{workflow_id}"
    new_files = [path for path in video_files(record_dir) if path not in before_files]
    renamed: list[str] = []
    for index, path in enumerate(new_files, start=1):
        suffix = path.suffix or ".webm"
        extra = "" if index == 1 else f"-{index}"
        target = record_dir / f"{stem}{extra}{suffix}"
        if target.exists() and target != path:
            target.unlink()
        if path != target:
            path.rename(target)
        renamed.append(target.name)
    return renamed


def build_llm(model: str) -> Any:
    """Build the browser-use LLM for AGENT_BROWSER_PROVIDER."""
    provider = os.environ.get("AGENT_BROWSER_PROVIDER", "openai").strip().lower()

    if provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise SystemExit("ANTHROPIC_API_KEY is required")
        from browser_use import ChatAnthropic

        return ChatAnthropic(model=model, api_key=api_key)

    if provider != "openai":
        raise SystemExit(f"browser-use accepts only openai or anthropic, got: {provider}")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required")
    base_url = None

    from browser_use import ChatOpenAI

    llm_kwargs = {
        "model": model,
        "api_key": api_key,
        "temperature": float(os.environ.get("AGENT_BROWSER_TEMPERATURE", "0")),
        "top_p": float(os.environ.get("AGENT_BROWSER_TOP_P", "1")),
        "max_completion_tokens": int(os.environ.get("AGENT_BROWSER_MAX_TOKENS", "4096")),
        "timeout": float(os.environ.get("AGENT_BROWSER_LLM_TIMEOUT", "600")),
    }
    if base_url:
        llm_kwargs["base_url"] = base_url
    seed = os.environ.get("AGENT_BROWSER_SEED", "").strip()
    if seed:
        llm_kwargs["seed"] = int(seed)
    return ChatOpenAI(**llm_kwargs)


async def run_one_workflow(
    workflow: dict[str, Any],
    base_url: str,
    llm: Any,
    max_steps: int,
    output_dir: Path,
    setup_dir: Path,
    phase: str,
    attempt: int,
    harness: Any,
) -> dict[str, Any]:
    """One workflow: setup, harness run, then a structured verdict."""
    from browser_use import Browser, BrowserProfile

    workflow_id = workflow["id"]
    setup = workflow.get("setup") or []
    setup_dir.mkdir(parents=True, exist_ok=True)
    state_path = setup_dir / f".setup-{workflow_id}.json"
    await apply_setup(base_url, setup, state_path)
    os.environ["EVAL_PHASE"] = phase
    os.environ["EVAL_WORKFLOW_ID"] = str(workflow_id)
    os.environ["EVAL_ATTEMPT"] = str(attempt)
    os.environ["EVAL_OUTPUT_DIR"] = str(output_dir)
    if evaluation_harness_name() == "codex":
        artifact = output_dir / "logs" / "codex" / f"{phase}-{workflow_id}" / str(attempt)
        artifact.mkdir(parents=True, exist_ok=True)
        os.environ["CODEX_ARTIFACT_DIR"] = str(artifact)

    chromium_path = os.environ.get("CHROME_PATH", "/usr/bin/chromium")
    window_size = workflow_viewport(workflow)
    profile_kwargs: dict[str, Any] = {
        "headless": True,
        "allowed_domains": ["127.0.0.1", "localhost"],
        "window_size": window_size,
        "chromium_sandbox": False,
        "args": ["--no-sandbox", "--disable-dev-shm-usage"],
    }
    if Path(chromium_path).exists():
        profile_kwargs["executable_path"] = chromium_path
    if state_path.exists() and state_path.stat().st_size > 2:
        profile_kwargs["storage_state"] = str(state_path)

    record_dir: Path | None = None
    recording_before: set[Path] = set()
    if recording_enabled():
        record_dir = output_dir / "recordings"
        record_dir.mkdir(parents=True, exist_ok=True)
        recording_before = set(video_files(record_dir))
        # Fields shared by browser-use and Playwright.
        profile_kwargs["record_video_dir"] = str(record_dir)
        profile_kwargs["save_recording_path"] = str(record_dir)

    try:
        browser_profile = BrowserProfile(**profile_kwargs)
    except TypeError:
        # Older BrowserProfile builds reject recording fields.
        profile_kwargs.pop("record_video_dir", None)
        profile_kwargs.pop("save_recording_path", None)
        browser_profile = BrowserProfile(**profile_kwargs)

    browser_kwargs: dict[str, Any] = {"browser_profile": browser_profile}
    if record_dir is not None:
        browser_kwargs["record_video_dir"] = record_dir
    try:
        browser = Browser(**browser_kwargs)
    except TypeError:
        browser = Browser(browser_profile=browser_profile)

    llm_timeout = int(os.environ.get("AGENT_BROWSER_LLM_TIMEOUT", "600"))
    step_timeout = int(os.environ.get("AGENT_BROWSER_STEP_TIMEOUT", str(llm_timeout + 120)))
    agent = harness.build_agent(
        task=build_task(base_url, workflow),
        llm=llm,
        browser=browser,
        llm_timeout=llm_timeout,
        step_timeout=step_timeout,
    )

    history = None
    final_text = ""
    verdict: dict[str, Any] = {"result": "uncertain", "substeps": []}
    result_value = "uncertain"
    try:
        history = await agent.run(max_steps=max_steps)
        if hasattr(history, "final_result"):
            final_text = history.final_result() or ""
        elif hasattr(history, "final_answer"):
            final_text = history.final_answer() or ""

        verdict = extract_json_blob(final_text)
        result_value = workflow_result(verdict)
    finally:
        try:
            await browser.close()
        except Exception:
            pass

    result: dict[str, Any] = {
        "id": workflow_id,
        "category": workflow.get("category"),
        "purpose": workflow.get("purpose"),
        "result": result_value,
        "agent_result": verdict,
        "agent_text": final_text,
        "llm_audit": extract_llm_audit(history),
    }
    if record_dir is not None:
        files = rename_recording_files(record_dir, recording_before, workflow, phase)
        result["recordings"] = {
            "dir": str(record_dir.relative_to(output_dir)),
            "files": files,
        }
        if files:
            print(f"  recording: {result['recordings']['dir']}/{{{', '.join(files)}}}", flush=True)
        else:
            print(f"  recording: {result['recordings']['dir']}/ (no video yet)", flush=True)
    return result


async def main_async(args: argparse.Namespace) -> int:
    configure_eval_logging()

    workflows_path = Path(args.workflows)
    output_dir = Path(args.output_dir)
    setup_dir = Path(args.setup_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    setup_dir.mkdir(parents=True, exist_ok=True)

    base_url = os.environ.get("BASE_URL", "http://127.0.0.1:13200")
    harness_name = evaluation_harness_name()
    harness = load_harness(harness_name)
    print(f"  EVALUATION_HARNESS={harness_name}", flush=True)
    if harness_name == "browser-use":
        model = os.environ.get("AGENT_BROWSER_MODEL") or os.environ.get("EVALUATION_MODEL")
        if not model:
            raise SystemExit(
                "AGENT_BROWSER_MODEL or EVALUATION_MODEL is required"
            )
        llm = build_llm(model)
    else:
        llm = None
    max_steps = int(os.environ.get("AGENT_BROWSER_MAX_STEPS", "50"))
    workflow_filter = os.environ.get("AGENT_BROWSER_WORKFLOW_IDS", "").strip()
    category_filter_raw = os.environ.get("AGENT_BROWSER_WORKFLOW_CATEGORIES", "").strip()
    if recording_enabled():
        print("  AGENT_BROWSER_RECORD=on; videos go to output/recordings/", flush=True)
    all_workflows = load_workflows(workflows_path)
    workflows = all_workflows

    if category_filter_raw:
        allowed_categories = parse_category_filter(category_filter_raw)
        print(
            "  AGENT_BROWSER_WORKFLOW_CATEGORIES="
            + ",".join(sorted(allowed_categories)),
            flush=True,
        )
        workflows = [item for item in workflows if item.get("category") in allowed_categories]
        if not workflows:
            raise SystemExit(
                f"AGENT_BROWSER_WORKFLOW_CATEGORIES matched no workflow: {category_filter_raw}"
            )

    if workflow_filter:
        allowed = {item.strip() for item in workflow_filter.split(",") if item.strip()}
        workflows = [item for item in workflows if item.get("id") in allowed]
        if not workflows:
            raise SystemExit(f"AGENT_BROWSER_WORKFLOW_IDS matched no workflow: {workflow_filter}")

    from parse_results import hard_uncertain, write_cell_results

    phase = args.phase
    logs_dir = output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    result_json_path = logs_dir / f"{phase}-result.json"
    result_jsonl_path = output_dir / "result.jsonl"

    to_run: list[dict[str, Any]] = []
    for workflow in workflows:
        category = workflow.get("category")
        if category not in ("target", "preservation"):
            continue
        to_run.append(workflow)

    if not to_run:
        print(f"  phase {phase} has no runnable cells; skipping", flush=True)
        return 0

    pending_by_category: dict[str, int] = {}
    for workflow in to_run:
        category = str(workflow.get("category"))
        pending_by_category[category] = pending_by_category.get(category, 0) + 1

    results_by_category: dict[str, list[dict[str, Any]]] = {}
    results: list[dict[str, Any]] = []
    for workflow in to_run:
        print(flush=True)
        print(format_workflow_header(phase, workflow, all_workflows), flush=True)
        item: dict[str, Any] | None = None
        for attempt in range(1, 4):
            if attempt > 1:
                print(f"  workflow infrastructure retry {attempt}/3", flush=True)
            try:
                item = await run_one_workflow(
                    workflow,
                    base_url,
                    llm,
                    max_steps,
                    output_dir,
                    setup_dir,
                    phase,
                    attempt,
                    harness,
                )
            except Exception as exc:
                print(f"  {phase}-workflow error: {exc}", flush=True)
                purpose = workflow.get("purpose")
                item = {
                    "id": str(workflow.get("id") or "unknown"),
                    "category": workflow.get("category"),
                    "purpose": purpose if isinstance(purpose, str) else "",
                    "result": "uncertain",
                    "agent_result": {"result": "uncertain", "error": str(exc)},
                    "agent_text": "",
                    "llm_audit": dict(MISSING_LLM_AUDIT),
                }
            purpose = item.get("purpose")
            if not isinstance(purpose, str):
                item["purpose"] = "" if purpose is None else str(purpose)
            item["id"] = str(item.get("id") or "unknown")
            item["attempts"] = attempt
            if item.get("result") != "uncertain":
                break
            if hard_uncertain(item):
                if attempt == 3:
                    break
                print("  workflow result: uncertain (infrastructure or JSON); retrying this workflow", flush=True)
                continue
            print("  workflow result: uncertain (agent verdict; phase retry decides)", flush=True)
            break
        category = str(item.get("category") or workflow.get("category"))
        results.append(item)
        results_by_category.setdefault(category, []).append(item)
        print(f"  {phase}-workflow result: {item['result']}", flush=True)
        if len(results_by_category[category]) == pending_by_category.get(category, 0):
            write_cell_results(
                result_json_path,
                result_jsonl_path,
                phase,
                category,
                results_by_category[category],
            )
            print(f"  wrote cell {phase}-{category}", flush=True)

    non_pass = [item for item in results if item.get("result") != "pass"]
    return 1 if non_pass else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run evaluation workflows")
    parser.add_argument("--phase", required=True)
    parser.add_argument("--workflows", default="/evaluation/workflows.yaml")
    parser.add_argument("--output-dir", default="/evaluation/output")
    parser.add_argument(
        "--setup-dir",
        default=os.environ.get("SETUP_DIR", "/evaluation/temporary/setup"),
        help="Playwright storage_state directory, removed with the temporary dir",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"baseline|reference|prediction-specified|prediction-scoped", args.phase):
        raise SystemExit(f"unknown evaluation phase: {args.phase}")
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
