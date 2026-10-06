"""Modal VM (vm_runtime): one VM per shard, cases serial inside. Dockerd stays up."""

import io
import json
import os
import tarfile
from pathlib import Path

import modal

from data import eval_row, normalize_evaluation_phases, pred_row

RUNTIME = Path(__file__).resolve().parent / "runtime"


def env_flag(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in ("0", "false", "no", "off"):
        return False
    if text in ("1", "true", "yes", "on"):
        return True
    return default


ACCOUNT_KEYS = {
    ("codex", "api"): [],
    ("claude-code", "api"): ["ANTHROPIC_API_KEY"],
    ("cursor", "api"): ["CURSOR_API_KEY"],
    ("kimi-code", "api"): [],
    ("gemini-cli", "api"): ["GEMINI_API_KEY"],
    ("deepseek-harness", "api"): ["DEEPSEEK_API_KEY"],
    ("muse-code", "api"): [],
    ("browser-use", "api"): [],
    ("codex", "subscription"): ["CODEX_AUTH_JSON"],
}

OFFICIAL_PROVIDERS = {
    "codex": {"openai"},
    "claude-code": {"anthropic"},
    "cursor": {"cursor"},
    "kimi-code": {"kimi"},
    "gemini-cli": {"gemini", "google"},
    "deepseek-harness": {"deepseek"},
    "muse-code": {"meta", "muse"},
    "browser-use": {"openai", "anthropic"},
}

PASS_ENV = (
    "GHCR_TOKEN",
    "GHCR_USER",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CODEX_AUTH_JSON",
    "CURSOR_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY",
    "META_API_KEY",
    "MODEL_API_KEY",
)

# Any listed variable is enough. Skip this check when every account sets api_key_env.
PROVIDER_ANY = {
    "openai": (("OPENAI_API_KEY", "CODEX_API_KEY"), True, "OPENAI_API_KEY"),
    "anthropic": (("ANTHROPIC_API_KEY",), True, "ANTHROPIC_API_KEY"),
    "cursor": (("CURSOR_API_KEY",), True, "CURSOR_API_KEY"),
    "kimi": (("KIMI_API_KEY", "MOONSHOT_API_KEY"), True, "KIMI_API_KEY"),
    "gemini": (("GEMINI_API_KEY",), True, "GEMINI_API_KEY"),
    "google": (("GEMINI_API_KEY",), True, "GEMINI_API_KEY"),
    "deepseek": (("DEEPSEEK_API_KEY",), True, "DEEPSEEK_API_KEY"),
    "meta": (("META_API_KEY", "MODEL_API_KEY"), True, "META_API_KEY"),
    "muse": (("META_API_KEY", "MODEL_API_KEY"), True, "META_API_KEY"),
}


def required_keys(account):
    return ACCOUNT_KEYS.get((account["harness"], account.get("auth", "api")), [])


def stage_accounts(config, stage):
    names = (config.get(stage) or {}).get("accounts") or []
    accounts = config.get("accounts") or {}
    return [accounts[name] for name in names if name in accounts]


def require_official_provider(config, stage):
    section = config.get(stage) or {}
    harness = (section.get("harness") or "").strip()
    allowed = OFFICIAL_PROVIDERS.get(harness)
    if not allowed:
        return
    provider = (section.get("provider") or "").strip().lower()
    if provider not in allowed:
        names = ", ".join(sorted(allowed))
        raise ValueError(f"{stage} harness {harness} only accepts official provider: {names}")


def require_provider_env(provider, accounts, missing):
    spec = PROVIDER_ANY.get((provider or "").strip().lower())
    if not spec:
        return
    keys, skip_if_keyed, label = spec
    if skip_if_keyed and accounts and all((account.get("api_key_env") or "").strip() for account in accounts):
        return
    if any(os.environ.get(key, "").strip() for key in keys):
        return
    if label not in missing:
        missing.append(label)


def subscription_auth(account):
    """Read only the selected account; never put credential contents in config/artifacts."""
    path = account.get("auth_file")
    raw = Path(path).expanduser().read_text() if path else os.environ.get(
        account.get("auth_env", "CODEX_AUTH_JSON"), ""
    )
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError("Codex subscription requires a valid auth_file or auth_env") from None
    if not isinstance(value, dict) or not isinstance(value.get("tokens"), dict) or not value["tokens"].get("access_token"):
        raise ValueError("Codex subscription credentials have no access token")
    return raw


def claude_oauth(account):
    """Load one setup-token credential without embedding it in run metadata."""
    path = account.get("auth_file")
    token = Path(path).expanduser().read_text().strip() if path else os.environ.get(
        account.get("auth_env", "CLAUDE_CODE_OAUTH_TOKEN"), ""
    ).strip()
    if not token.startswith("sk-ant-oat") or any(c.isspace() for c in token):
        raise ValueError("Claude subscription requires a setup-token auth_file or auth_env")
    return token


def require_host_env(config, stages):
    missing = []
    if not os.environ.get("GHCR_TOKEN", "").strip():
        missing.append("GHCR_TOKEN")
    if not os.environ.get("HF_TOKEN", "").strip():
        missing.append("HF_TOKEN")
    for stage in stages:
        require_official_provider(config, stage)
        accounts = stage_accounts(config, stage)
        if not accounts or not all(account.get("auth") == "subscription" for account in accounts):
            require_provider_env((config.get(stage) or {}).get("provider"), accounts, missing)
        for name in config[stage]["accounts"]:
            account = config["accounts"][name]
            if account.get("auth") == "subscription" and account["harness"] == "codex":
                subscription_auth(account)
                continue
            if account.get("auth") == "subscription" and account["harness"] == "claude-code":
                claude_oauth(account)
                if stage == "prediction" and not env_flag(config[stage].get("egress"), True):
                    raise ValueError("Claude subscription prediction requires egress isolation")
                if config[stage].get("provider") != "anthropic":
                    raise ValueError("Claude subscription requires provider=anthropic")
                continue
            src = (account.get("api_key_env") or "").strip()
            if src and account.get("auth", "api") == "api":
                if not os.environ.get(src, "").strip() and src not in missing:
                    missing.append(src)
            else:
                for key in required_keys(account):
                    if not os.environ.get(key, "").strip() and key not in missing:
                        missing.append(key)
    if missing:
        raise SystemExit("Export before launch (not Modal Secrets): " + ", ".join(missing))


def host_credentials(account):
    env = {}
    for key in PASS_ENV:
        value = os.environ.get(key)
        if value:
            env[key] = value
    env.pop("CODEX_AUTH_JSON", None)
    if account.get("auth") == "subscription" and account["harness"] == "codex":
        env["CODEX_AUTH_JSON"] = subscription_auth(account)
    if account.get("auth") == "subscription" and account["harness"] == "claude-code":
        env["CLAUDE_CODE_OAUTH_TOKEN"] = claude_oauth(account)
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
            env.pop(key, None)
    src = (account.get("api_key_env") or "").strip()
    dest = (account.get("api_key_name") or src).strip()
    if src and account.get("auth", "api") == "api":
        value = os.environ.get(src, "").strip()
        if value:
            env[dest] = value
    return env


def vm_image():
    return (
        modal.Image.from_registry("ubuntu:24.04")
        .env({"DEBIAN_FRONTEND": "noninteractive"})
        .apt_install(
            "docker.io",
            "docker-buildx",
            "ca-certificates",
            "python3",
            "python3-yaml",
            "git",
            "curl",
            "iptables",
            "iproute2",
        )
        .add_local_dir(RUNTIME, "/runtime", copy=True)
    )


class Backend:
    def __init__(self, config):
        self.config = config
        profile = config["modal"].get("profile")
        if profile:
            current = os.environ.get("MODAL_PROFILE")
            if current and current != profile:
                raise RuntimeError(
                    f"MODAL_PROFILE={current} does not match config {profile}"
                )
            os.environ.setdefault("MODAL_PROFILE", profile)
        self.app = modal.App.lookup(config["modal"].get("app", "sweeper-bench"), create_if_missing=True)
        self.image = vm_image()

    def create(self, dest, account):
        dest.mkdir(parents=True, exist_ok=True)
        settings = self.config["modal"]
        eval_cfg = self.config.get("evaluation", {})
        env = {
            "RUNTIME": "/runtime",
            "GHCR_REGISTRY": self.config.get("images", {}).get("registry", "ghcr.io"),
            "GHCR_OWNER": self.config.get("images", {}).get("owner", "evigbyen"),
            "GHCR_TAG": self.config.get("images", {}).get("tag", "alpha"),
            "GHCR_USER": self.config.get("images", {}).get("user", "evigbyen"),
            "PRED_EGRESS": "1" if self.config.get("prediction", {}).get("egress", True) else "0",
            "EVAL_DROP_CACHES": "1" if env_flag(eval_cfg.get("drop_caches", 1), True) else "0",
            "EVAL_SKIP_FAILED_TASKS": "1"
            if env_flag(eval_cfg.get("skip_failed_tasks", 1), True)
            else "0",
        }
        env.update(host_credentials(account))
        sb = modal.Sandbox.create(
            "sleep",
            "infinity",
            app=self.app,
            image=self.image,
            timeout=settings.get("timeout_seconds", 86400),
            cpu=settings.get("cpu", 8),
            memory=settings.get("memory_mb", 16384),
            region=settings.get("region", "us-east"),
            env=env,
            experimental_options={"vm_runtime": True},
        )
        (dest / "sandbox.json").write_text(
            json.dumps({"id": sb.object_id, "vm_runtime": True}, indent=2) + "\n"
        )
        return sb

    def drain(self, process, dest, name, timeout):
        dest.mkdir(parents=True, exist_ok=True)
        stdout = dest / f"{name}.stdout.log"
        stderr = dest / f"{name}.stderr.log"

        def pump(stream, path):
            with path.open("w") as handle:
                for line in stream:
                    handle.write(line)
                    handle.flush()

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(pump, process.stdout, stdout)
            b = pool.submit(pump, process.stderr, stderr)
            process.wait()
            a.result()
            b.result()
        if process.returncode:
            raise RuntimeError(
                f"{name} failed ({process.returncode}): {(stderr.read_text() or stdout.read_text())[-800:]}"
            )
        return process.returncode

    def boot(self, sb, dest):
        process = sb.exec("bash", "/runtime/install.sh", timeout=1800)
        self.drain(process, dest, "boot", 1800)

    def _exec_case(self, sb, stage, row, account, dest, incoming=None):
        pred = self.config.get("prediction") or {}
        ev = self.config.get("evaluation") or {}
        payload = {
            "stage": stage,
            "auth": account.get("auth", "api"),
            "row": pred_row(row, pred.get("level", "scoped")) if stage == "prediction" else eval_row(row),
            "prediction": {
                "harness": pred.get("harness", ""),
                "provider": pred.get("provider", "openai"),
                "model": pred.get("model", ""),
                "effort": pred.get("effort", "xhigh"),
                "level": pred.get("level", "scoped"),
                "time_budget": pred.get("time_budget", "unlimited"),
                "prompt_templates": pred.get("prompt_templates", "browser"),
            },
            "evaluation": {
                "harness": ev.get("harness", ""),
                "provider": ev.get("provider", "openai"),
                "model": ev.get("model", ""),
                "effort": ev.get("effort", "xhigh"),
                "phases": normalize_evaluation_phases(self.config) if ev else [],
                "categories": ev.get("categories", "target,preservation"),
                "record": str(ev.get("record", 0)),
            },
        }
        sb.exec("bash", "-lc", "rm -rf /incoming /artifacts.tgz /task.json && mkdir -p /incoming").wait()
        sb.filesystem.write_text(json.dumps(payload, ensure_ascii=False), "/task.json")
        if incoming:
            for name, path in incoming.items():
                sb.filesystem.write_bytes(Path(path).read_bytes(), f"/incoming/{name}.patch")
        prev = dest / "artifacts"
        if (prev / "result.jsonl").is_file() or list((prev / "logs").glob("*-result.json")) or list(prev.glob("prediction-*.patch")):
            import io

            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                for path in prev.rglob("*"):
                    if path.is_file():
                        tar.add(path, arcname=str(path.relative_to(prev)))
            sb.filesystem.write_bytes(buf.getvalue(), "/incoming/prev-output.tgz")
        case_timeout = self.config[stage].get("timeout_seconds", 28800 if stage == "prediction" else 10800)
        process = sb.exec(
            "python3",
            "/runtime/worker.py",
            timeout=case_timeout,
        )
        worker_error = None
        try:
            self.drain(process, dest, "worker", case_timeout)
        except Exception as exc:
            worker_error = exc
        try:
            self.collect(sb, dest)
        except Exception:
            if worker_error is None:
                raise
        if worker_error:
            raise worker_error

    def collect(self, sb, dest):
        artifacts = dest / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        try:
            blob = sb.filesystem.read_bytes("/artifacts.tgz")
        except Exception as exc:
            (dest / "collection-error.txt").write_text(str(exc))
            raise
        (dest / "artifacts.tgz").write_bytes(blob)
        with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
            tar.extractall(artifacts, filter="data")
