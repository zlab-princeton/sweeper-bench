"""Run prediction and evaluation cases inside one VM."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

RUNTIME = Path(os.environ.get("RUNTIME", "/runtime"))
WORK = Path("/work")
ART = Path("/artifacts.tgz")


def run(args, check=True, env=None, cwd=None):
    print("+", " ".join(str(a) for a in args), flush=True)
    return subprocess.run(args, check=check, env=env, cwd=cwd)


def ghcr(name):
    return "{}/{}/{}:{}".format(
        os.environ.get("GHCR_REGISTRY", "ghcr.io"),
        os.environ.get("GHCR_OWNER", "evigbyen"),
        name,
        os.environ.get("GHCR_TAG", "alpha"),
    )


def pull(remote, local):
    run(["docker", "pull", remote])
    run(["docker", "tag", remote, local])


def docker_gw():
    p = subprocess.run(
        ["docker", "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}"],
        capture_output=True,
        text=True,
    )
    gw = (p.stdout or "").strip()
    return gw or "172.17.0.1"


def start_pred_egress():
    if os.environ.get("PRED_EGRESS", "1") != "1":
        return None, ""
    portfile = Path("/tmp/pred-egress.port")
    portfile.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [
            "python3",
            str(RUNTIME / "pred_egress.py"),
            "--allowlist",
            str(RUNTIME / "allowlist.txt"),
            "--upstream",
            os.environ.get("PRED_EGRESS_UPSTREAM", ""),
            "--bind",
            "0.0.0.0",
            "--port",
            "0",
            "--portfile",
            str(portfile),
        ]
    )
    for _ in range(40):
        if portfile.is_file() and portfile.stat().st_size:
            break
        if proc.poll() is not None:
            raise RuntimeError("pred egress proxy failed")
        time.sleep(0.1)
    port = portfile.read_text().strip()
    url = f"http://{docker_gw()}:{port}"
    print(f"pred egress {url}", flush=True)
    try:
        run(["bash", str(RUNTIME / "pred_net.sh"), "lock"])
    except Exception:
        proc.terminate()
        proc.wait(timeout=10)
        raise
    return proc, url


def stop_pred_egress(proc):
    if proc is None:
        return
    run(["bash", str(RUNTIME / "pred_net.sh"), "unlock"], check=False)
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


def git(dest, *args, check=True):
    return run(["git", "-C", str(dest), *args], check=check)


def workspace_has_extra_history(dest, commit, gold=""):
    if not (dest / ".git").is_dir():
        return False
    head = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
    )
    if (head.stdout or "").strip() != commit:
        return True
    if gold:
        probe = subprocess.run(
            ["git", "-C", str(dest), "cat-file", "-e", f"{gold}^{{commit}}"],
            capture_output=True,
        )
        if probe.returncode == 0:
            return True
    count = subprocess.run(
        ["git", "-C", str(dest), "rev-list", "--count", "--all"],
        capture_output=True,
        text=True,
    )
    try:
        return int((count.stdout or "0").strip() or "0") > 1
    except ValueError:
        return True


def flatten_clone(dest, commit):
    git(dest, "checkout", "--force", "--detach", commit)
    git(dest, "remote", "remove", "origin", check=False)
    refs = subprocess.check_output(
        ["git", "-C", str(dest), "for-each-ref", "--format=%(refname)"],
        text=True,
    )
    for ref in refs.splitlines():
        ref = ref.strip()
        if ref:
            git(dest, "update-ref", "-d", ref, check=False)
    git(dest, "reflog", "expire", "--expire=now", "--all")
    git(dest, "gc", "--prune=now")


def clone_workspace(repo, commit, dest, gold="", full_clone_fallback=False):
    if dest.exists() and workspace_has_extra_history(dest, commit, gold):
        print(f"workspace has extra git history, rebuild: {dest}", flush=True)
        shutil.rmtree(dest)
    if not (dest / ".git").is_dir():
        print(f"shallow clone {commit} -> {dest}", flush=True)
        dest.mkdir(parents=True, exist_ok=True)
        run(["git", "init"], cwd=dest)
        git(dest, "remote", "add", "origin", repo)
        fetched = git(dest, "fetch", "--depth", "1", "origin", commit, check=False)
        if fetched.returncode == 0:
            git(dest, "checkout", "--force", "FETCH_HEAD")
        elif full_clone_fallback:
            print("shallow fetch failed, full clone then flatten", flush=True)
            shutil.rmtree(dest)
            run(["git", "clone", repo, str(dest)])
            git(dest, "checkout", "--force", "--detach", commit)
            flatten_clone(dest, commit)
        else:
            shutil.rmtree(dest, ignore_errors=True)
            raise RuntimeError(f"shallow fetch failed: {commit} from {repo}")
    run(["git", "config", "--global", "--add", "safe.directory", str(dest)])
    reset_workspace(dest, commit)
    if gold:
        probe = subprocess.run(
            ["git", "cat-file", "-e", f"{gold}^{{commit}}"],
            cwd=dest,
            capture_output=True,
        )
        if probe.returncode == 0:
            raise RuntimeError(f"workspace can see reference_commit {gold}")
    git(dest, "remote", "remove", "origin", check=False)


IMAGE_APPLY_EXCLUDES = (
    "--exclude=*.png",
    "--exclude=*.PNG",
    "--exclude=*.jpg",
    "--exclude=*.jpeg",
    "--exclude=*.JPG",
    "--exclude=*.JPEG",
    "--exclude=*.gif",
    "--exclude=*.GIF",
    "--exclude=*.webp",
    "--exclude=*.WEBP",
    "--exclude=*.ico",
    "--exclude=*.ICO",
    "--exclude=*.bmp",
    "--exclude=*.BMP",
)


def reset_workspace(dest, commit, patch=None):
    run(["git", "reset", "--hard", commit], cwd=dest)
    run(["git", "clean", "-fdx"], cwd=dest)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=dest, text=True).strip()
    if head != commit:
        raise RuntimeError(f"HEAD {head} != {commit}")
    if not patch:
        return
    first = run(["git", "apply", str(patch)], cwd=dest, check=False)
    if first.returncode == 0:
        return
    print(f"git apply failed; retry excluding images: {patch}", flush=True)
    run(["git", "reset", "--hard", commit], cwd=dest)
    run(["git", "clean", "-fdx"], cwd=dest)
    run(["git", "apply", *IMAGE_APPLY_EXCLUDES, str(patch)], cwd=dest)


def render_prompt(row, level, dest, prompt_templates="browser"):
    if level not in row["task"]:
        raise KeyError(f"task has no {level}")
    body = str(row["task"][level]).strip() + "\n"
    path = RUNTIME / "prompts" / f"task-templates-{prompt_templates}.md"
    if not path.is_file():
        raise FileNotFoundError(f"prediction prompt template missing: {path}")
    lines = path.read_text().splitlines(True)
    out = []
    header = True
    for line in lines:
        if header:
            if line.startswith("#") or not line.strip():
                continue
            header = False
        if "{{TASK_DESCRIPTION}}" in line:
            out.append(body)
            continue
        out.append(line)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("".join(out))


def parse_results(*args, check=True):
    result = subprocess.run(
        ["python3", str(RUNTIME / "parse_results.py"), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    err = result.stderr or ""
    crashed = result.returncode != 0 or "Traceback (most recent call last)" in err
    if check and crashed:
        raise RuntimeError(
            f"parse_results {' '.join(args)} failed ({result.returncode}): "
            f"{(err or result.stdout)[-500:]}"
        )
    return result


def env_with(extra):
    env = os.environ.copy()
    env.update({k: str(v) for k, v in extra.items() if v is not None})
    return env


def run_product(workspace, container, image, proxy=""):
    extra = {
        "WORKSPACE_DIR": str(workspace),
        "PRODUCT_CONTAINER": container,
        "PRODUCT_IMAGE": image,
        "PRED_EGRESS_PROXY_URL": proxy,
        "RUNTIME": str(RUNTIME),
    }
    run(["bash", str(RUNTIME / "product.sh")], env=env_with(extra))


def stop_product(*names):
    if names:
        run(["docker", "rm", "-f", *names], check=False)
    run(["bash", str(RUNTIME / "release-product-port.sh")], check=False)


def ensure_product_port_free():
    run(["bash", str(RUNTIME / "release-product-port.sh")])


def phase_between_cleanup(temporary, after_phase):
    print(f"=== phase cleanup after {after_phase} ===", flush=True)
    setup = Path(temporary) / "setup"
    removed = 0
    if setup.is_dir():
        for snap in setup.glob(".setup-*"):
            snap.unlink()
            removed += 1
        print(f"  temporary/setup: removed {removed} snapshots", flush=True)
    else:
        print("  temporary/setup: missing, skip", flush=True)
    if os.environ.get("EVAL_DROP_CACHES", "1") != "0":
        drop = Path("/proc/sys/vm/drop_caches")
        if os.access(drop, os.W_OK):
            os.sync()
            drop.write_text("3")
            print("  page cache dropped", flush=True)
        else:
            print("  page cache: no root, skip", flush=True)
    free = shutil.which("free")
    if free:
        print(subprocess.check_output([free, "-h"], text=True), flush=True)


def local_product(cid, kind):
    return f"{cid}-{kind}:{os.environ.get('GHCR_TAG', 'alpha')}"


def cleanup_case(cid):
    stop_product()
    run(
        [
            "bash",
            "-lc",
            "docker ps -aq --filter status=exited --filter status=dead "
            "| xargs -r docker rm -f",
        ],
        check=False,
    )
    tag = os.environ.get("GHCR_TAG", "alpha")
    run(["docker", "image", "rm", f"{cid}-prediction:{tag}"], check=False)
    run(["docker", "image", "rm", f"{cid}-evaluation:{tag}"], check=False)
    shutil.rmtree(WORK / cid, ignore_errors=True)


def predict_case(task, row):
    cid = row["id"]
    ensure_product_port_free()
    workspace = WORK / cid / "workspace"
    output = WORK / cid / "output"
    output.mkdir(parents=True, exist_ok=True)
    product_image = local_product(cid, "prediction")
    pull(ghcr(f"{cid}-prediction"), product_image)
    harness = task["prediction"]["harness"]
    agent_image = f"prediction-{harness}:latest"
    pull(ghcr(f"prediction-{harness}"), agent_image)
    level = task["prediction"].get("level") or "scoped"
    container = f"{cid}-product-{level}"
    prompt = WORK / cid / f"task-prompt-{level}.md"
    proxy_proc = None
    try:
        clone_workspace(
            row["repo"],
            row["base_commit"],
            workspace,
            row.get("reference_commit") or "",
            full_clone_fallback=True,
        )
        proxy_proc, proxy_url = start_pred_egress()
        reset_workspace(workspace, row["base_commit"])
        render_prompt(
            row,
            level,
            prompt,
            task["prediction"].get("prompt_templates") or "browser",
        )
        try:
            run_product(workspace, container, product_image, proxy_url)
            extra = {
                "WORKSPACE_DIR": str(workspace),
                "TASK_PROMPT_FILE": str(prompt),
                "OUTPUT_DIR": str(output),
                "AGENT_IMAGE": agent_image,
                "PRODUCT_CONTAINER": container,
                "PREDICTION_HARNESS": harness,
                "PREDICTION_AUTH": task.get("auth", "api"),
                "PREDICTION_PROVIDER": task["prediction"]["provider"],
                "PREDICTION_MODEL": task["prediction"]["model"],
                "PREDICTION_EFFORT": task["prediction"].get("effort", "xhigh"),
                "PREDICTION_WORK_MINUTES": str(task["prediction"].get("time_budget", "unlimited")),
                "PREDICTION_LEVEL": level,
                "PREDICTION_LOG_DIR": f"/workspace/output/logs-{level}",
                "PREDICTION_PATCH_FILE": f"/workspace/output/prediction-{level}.patch",
                "PRED_EGRESS_PROXY_URL": proxy_url,
                "RUNTIME": str(RUNTIME),
            }
            run(["bash", str(RUNTIME / "pred_container.sh")], env=env_with(extra))
        finally:
            stop_product(container)
        patch = output / f"prediction-{level}.patch"
        ok = patch.is_file() and "diff --git " in patch.read_text(errors="replace")
        if not ok:
            raise SystemExit(f"prediction patch missing or empty: {patch}")
    finally:
        # Preserve harness diagnostics even when prediction fails before patch creation.
        try:
            pack(output)
        finally:
            stop_pred_egress(proxy_proc)
            cleanup_case(cid)


def phase_patch(phase, cid, task):
    if phase == "baseline":
        return None
    if phase == "reference":
        uploaded = Path("/incoming/reference.patch")
        return uploaded if uploaded.is_file() else None
    if phase.startswith("prediction-"):
        uploaded = Path("/incoming") / f"{phase}.patch"
        if uploaded.is_file():
            return uploaded
        return WORK / cid / "output" / f"{phase}.patch"
    raise ValueError(f"unknown phase {phase}")


def evaluate_case(task, row):
    cid = row["id"]
    ensure_product_port_free()
    workspace = WORK / cid / "workspace"
    output = WORK / cid / "output"
    temporary = WORK / cid / "temporary"
    logs = output / "logs"
    output.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    temporary.mkdir(parents=True, exist_ok=True)
    prev = Path("/incoming/prev-output.tgz")
    if prev.is_file() and prev.stat().st_size:
        with tarfile.open(prev) as tar:
            tar.extractall(output, filter="data")
        logs.mkdir(parents=True, exist_ok=True)
    yaml_text = str(row.get("workflows") or "").strip()
    if not yaml_text:
        raise SystemExit("eval task missing workflows")
    auth_file = Path("/run/secrets/sweeper-codex-auth.json")
    if task.get("auth") == "subscription":
        auth_file.parent.mkdir(parents=True, exist_ok=True)
        auth_file.touch(mode=0o600, exist_ok=True)
        auth_file.write_text(os.environ["CODEX_AUTH_JSON"])
        auth_file.chmod(0o600)
    workflows = WORK / cid / "workflows.yaml"
    workflows.write_text(yaml_text, encoding="utf-8")
    product_image = local_product(cid, "evaluation")
    pull(ghcr(cid), product_image)
    eval_harness = task["evaluation"]["harness"]
    if eval_harness == "codex":
        eval_image = "evaluation-agent-codex:latest"
        pull(ghcr("evaluation-codex"), eval_image)
    else:
        eval_image = "evaluation-agent-browser-use:latest"
        pull(ghcr("evaluation-browser-use"), eval_image)
    clone_workspace(
        row["repo"],
        row["base_commit"],
        workspace,
        row.get("reference_commit") or "",
        full_clone_fallback=False,
    )
    categories = task["evaluation"].get("categories", "target,preservation")
    result_jsonl = output / "result.jsonl"
    runnable = []
    try:
        for phase in task["evaluation"]["phases"]:
            patch = phase_patch(phase, cid, task)
            if phase != "baseline" and (not patch or not Path(patch).is_file()):
                print(f"skip phase {phase}: missing patch", flush=True)
                continue
            runnable.append(phase)
            result_json = logs / f"{phase}-result.json"
            incomplete = parse_results(
                "incomplete-categories",
                "--phase",
                phase,
                "--result-json",
                str(result_json),
                "--result-jsonl",
                str(result_jsonl),
                "--workflows",
                str(workflows),
                "--categories",
                categories,
            )
            missing = (incomplete.stdout or "").strip()
            if not missing:
                print(f"skip phase {phase}: complete", flush=True)
                continue
            csv = missing.replace(" ", ",")
            parse_results(
                "clear-phase",
                "--phase",
                phase,
                "--result-json",
                str(result_json),
                "--result-jsonl",
                str(result_jsonl),
                "--categories",
                csv,
            )
            container = f"{cid}-product-{phase}"
            for attempt in range(1, 4):
                print(f"phase {phase} attempt {attempt}/3", flush=True)
                ensure_product_port_free()
                for snap in (temporary / "setup").glob(".setup-*"):
                    snap.unlink()
                reset_workspace(workspace, row["base_commit"], patch)
                try:
                    run_product(workspace, container, product_image)
                    extra = {
                        "EVAL_IMAGE": eval_image,
                        "BASE_URL": "http://127.0.0.1:13200",
                        "WORKFLOWS_PATH": str(workflows),
                        "EVAL_OUTPUT_DIR": str(output),
                        "EVAL_TEMPORARY_DIR": str(temporary),
                        "EVALUATION_HARNESS": eval_harness,
                        "EVALUATION_MODEL": task["evaluation"].get("model", ""),
                        "EVALUATION_CODEX_AUTH": task.get("auth", "api"),
                        "EVALUATION_CODEX_PROVIDER": task["evaluation"].get("provider", "openai"),
                        "CODEX_AUTH_FILE": str(auth_file),
                        "AGENT_BROWSER_MODEL": task["evaluation"].get("model", ""),
                        "AGENT_BROWSER_PROVIDER": task["evaluation"].get("provider", "openai"),
                        "AGENT_BROWSER_WORKFLOW_CATEGORIES": csv,
                        "AGENT_BROWSER_RECORD": task["evaluation"].get("record", "0"),
                        "CODEX_REASONING_EFFORT": task["evaluation"].get("effort", "xhigh"),
                        "RUNTIME": str(RUNTIME),
                    }
                    run(
                        [
                            "bash",
                            str(RUNTIME / "eval_container.sh"),
                            "python3",
                            "/evaluation/run-workflows.py",
                            "--phase",
                            phase,
                        ],
                        check=False,
                        env=env_with(extra),
                    )
                finally:
                    stop_product(container)
                uncertain = parse_results(
                    "agent-uncertain",
                    "--phase",
                    phase,
                    "--result-json",
                    str(result_json),
                    "--categories",
                    csv,
                )
                ids = (uncertain.stdout or "").strip()
                if ids and attempt < 3:
                    print(f"agent uncertain {ids}; retry class", flush=True)
                    parse_results(
                        "clear-phase",
                        "--phase",
                        phase,
                        "--result-json",
                        str(result_json),
                        "--result-jsonl",
                        str(result_jsonl),
                        "--categories",
                        csv,
                    )
                    continue
                break
            reset_workspace(workspace, row["base_commit"])
            phase_between_cleanup(temporary, phase)
        pack(output)
        if not runnable:
            print("skip task: no runnable evaluation phases", flush=True)
            return
        complete = parse_results(
            "task-complete",
            "--phases",
            " ".join(runnable),
            "--logs-dir",
            str(logs),
            "--result-jsonl",
            str(result_jsonl),
            "--workflows",
            str(workflows),
            "--categories",
            categories,
            check=False,
        )
        if complete.returncode != 0:
            print("skip task: evaluation cells incomplete", flush=True)
    finally:
        stop_product()
        cleanup_case(cid)


def pack(output):
    ART.unlink(missing_ok=True)
    with tarfile.open(ART, "w:gz") as tar:
        tar.add(output, arcname=".")


def main():
    task = json.loads(Path("/task.json").read_text())
    row = task["row"]
    stage = task["stage"]
    WORK.mkdir(parents=True, exist_ok=True)
    if stage == "prediction":
        predict_case(task, row)
    elif stage == "evaluation":
        evaluate_case(task, row)
    else:
        raise SystemExit(f"unknown stage {stage}")


if __name__ == "__main__":
    main()
