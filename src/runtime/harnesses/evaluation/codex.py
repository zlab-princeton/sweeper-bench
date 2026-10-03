"""Codex evaluation harness. Reuses browser setup and recording."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace


def build_agent(*, task, llm, browser, **kwargs):
    del llm, kwargs
    return CodexAgent(task=task, browser=browser)


def _auth_mode() -> str:
    raw = os.environ.get("EVALUATION_CODEX_AUTH", "subscription").strip().lower()
    if raw in ("subscription", "api"):
        return raw
    raise RuntimeError(f"unknown EVALUATION_CODEX_AUTH: {raw}; allowed: subscription, api")


def _write_api_config(home: Path, model: str) -> dict[str, str]:
    provider = os.environ.get("EVALUATION_CODEX_PROVIDER", "openai").strip().lower()
    if provider != "openai":
        raise RuntimeError(f"Codex evaluation only accepts official provider openai, got {provider}")
    base = "https://api.openai.com/v1"
    env_key = "OPENAI_API_KEY" if os.environ.get("OPENAI_API_KEY") else "CODEX_API_KEY"
    if not base:
        raise RuntimeError(f"Codex API provider={provider} is missing base_url")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    if not os.environ.get(env_key):
        raise RuntimeError(f"Codex API provider={provider} is missing {env_key}")
    (home / "config.toml").write_text(
        "\n".join(
            [
                f'model = "{model}"',
                'model_provider = "evaluation-provider"',
                'sandbox_mode = "danger-full-access"',
                'approval_policy = "never"',
                "",
                "[model_providers.evaluation-provider]",
                f'name = "{provider}"',
                f'base_url = "{base}"',
                f'env_key = "{env_key}"',
                'wire_api = "responses"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {"provider": provider, "base_url": base, "env_key": env_key}


class CodexAgent:
    def __init__(self, *, task, browser):
        self.task = task
        self.browser = browser

    async def run(self, max_steps):
        await self.browser.start()
        cdp_url = getattr(self.browser, "cdp_url", None)
        if callable(cdp_url):
            cdp_url = cdp_url()
        if not cdp_url:
            raise RuntimeError("evaluation browser did not expose a CDP endpoint")

        executable = os.environ.get("CODEX_EXECUTABLE", "/opt/agent-bin/codex")
        if not Path(executable).is_file():
            raise RuntimeError(
                f"Codex CLI not found in this image: {executable}"
            )

        output = Path(os.environ.get("CODEX_ARTIFACT_DIR") or "")
        if not output:
            output_root = Path(os.environ.get("EVAL_OUTPUT_DIR", "/evaluation/output"))
            phase = os.environ.get("EVAL_PHASE", "phase")
            workflow_id = os.environ.get("EVAL_WORKFLOW_ID", "workflow")
            attempt = os.environ.get("EVAL_ATTEMPT", "1")
            output = output_root / "logs" / "codex" / f"{phase}-{workflow_id}" / str(attempt)
        output.mkdir(parents=True, exist_ok=True)
        work = output / "workspace"
        work.mkdir(parents=True, exist_ok=True)

        instructions = (
            "Execute the supplied browser verification task. Do not modify the application.\n"
            f"Connect Playwright to the existing browser at {cdp_url}. Use its existing page and context.\n"
            "Use browser interactions, not source inspection or database/API shortcuts unless the workflow explicitly requests them.\n"
            "Do not close the browser or use external websites, web search, or subagents.\n"
            "Python Playwright is installed. Exit standalone scripts after printing their observations.\n"
            "Return the final JSON requested by the task.\n"
        )
        (work / "AGENTS.md").write_text(instructions, encoding="utf-8")
        (output / "prompt.txt").write_text(self.task, encoding="utf-8")

        home = Path(os.environ.get("CODEX_HOME", "/root/.codex"))
        home.mkdir(parents=True, exist_ok=True)
        model = os.environ.get("EVALUATION_MODEL", "gpt-5.6-luna")
        auth_mode = _auth_mode()
        api_meta: dict[str, str] = {}
        if auth_mode == "subscription":
            auth_src = os.environ.get("CODEX_AUTH_FILE", "").strip()
            if auth_src:
                auth = home / "auth.json"
                auth.write_bytes(Path(auth_src).read_bytes())
                auth.chmod(0o600)
        else:
            leftover = home / "auth.json"
            if leftover.exists():
                leftover.unlink()
            api_meta = _write_api_config(home, model)

        sessions = home / "sessions"
        prior = set(sessions.rglob("*.jsonl")) if sessions.exists() else set()
        final = output / "last-message.txt"
        command = [
            executable,
            "exec",
            "-C",
            str(work),
            "--skip-git-repo-check",
            "--model",
            model,
            "--sandbox",
            "danger-full-access",
        ]
        if auth_mode == "subscription":
            command.append("--ignore-user-config")
        else:
            command.extend(["--config", 'model_provider="evaluation-provider"'])
        command.extend(
            [
                "--config",
                "model_reasoning_effort=" + json.dumps(os.environ.get("CODEX_REASONING_EFFORT", "xhigh")),
                "--config",
                'approval_policy="never"',
                "--config",
                'web_search="disabled"',
                "--config",
                "features.multi_agent=false",
                "--output-last-message",
                str(final),
                "--json",
                "-",
            ]
        )
        record = {
            "command": command,
            "auth_mode": auth_mode,
            "api": api_meta,
            "upstream_max_steps": max_steps,
            "codex_timeout_seconds": int(os.environ.get("CODEX_TIMEOUT_SECONDS", "1800")),
            "started_at": time.time(),
            "note": "Codex uses a wall-clock limit; browser-use max_steps has no exact Codex equivalent.",
        }
        child_env = os.environ.copy()
        if auth_mode == "subscription":
            for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_AUTH_JSON"):
                child_env.pop(key, None)
        process = None
        try:
            with (output / "trajectory.jsonl").open("wb") as stdout, (output / "stderr.log").open("wb") as stderr:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                    env=child_env,
                )
                await asyncio.wait_for(
                    process.communicate(self.task.encode()),
                    record["codex_timeout_seconds"],
                )
                record["exit_code"] = process.returncode
            if process.returncode:
                raise RuntimeError(f"Codex exited with code {process.returncode}")
            answer = final.read_text(encoding="utf-8") if final.exists() else ""
            return SimpleNamespace(final_result=lambda: answer)
        except BaseException as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                if process.returncode is None:
                    try:
                        await asyncio.wait_for(process.wait(), 10)
                    except asyncio.TimeoutError:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        await process.wait()
            record["finished_at"] = time.time()
            (output / "execution.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            if sessions.exists():
                with tarfile.open(output / "native-sessions.tgz", "w:gz") as archive:
                    for path in sessions.rglob("*.jsonl"):
                        if path not in prior:
                            archive.add(path, arcname=str(path.relative_to(sessions)))
