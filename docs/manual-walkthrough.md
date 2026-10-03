# Manual walkthrough: images and agents directly on Modal

[README](../README.md) · [Guide](guide.md) · [Agents](agents.md) · [Design](design.md)

This walkthrough creates a Modal VM, pulls the published Docker images, starts a product, and invokes Codex directly. **It does not import this repository or call its runner, worker, harness adapters, or task-shell scripts.** All orchestration commands are shown below.

The example uses Codex subscription authentication, Astra xhigh for prediction, and Luna xhigh for verification. It is an operator-controlled demonstration, not a replacement for the benchmark's isolation and scoring implementation. In particular, the coding container gets Docker access and this example does not install the prediction egress firewall. See [Differences from the benchmark](#differences-from-the-benchmark).

## 1. Local setup

Choose a writable directory, such as scratch storage on an HPC cluster. No repository checkout or local Docker installation is required. You need access to Modal, the dataset and GHCR images, and the selected Codex models.

```bash
mkdir -p /absolute/writable/path/manual-sweeper
cd /absolute/writable/path/manual-sweeper
export XDG_CACHE_HOME="$PWD/.cache"
export HF_HOME="$XDG_CACHE_HOME/huggingface"
export PIP_CACHE_DIR="$XDG_CACHE_HOME/pip"
export TMPDIR="$XDG_CACHE_HOME/tmp"
mkdir -p "$TMPDIR"
python3 -m venv .venv
source .venv/bin/activate
pip install modal==1.5.5 huggingface-hub PyYAML
# For a new Modal login, keep its config in this writable directory:
export MODAL_CONFIG_PATH="$PWD/.cache/modal.toml"
modal token new
export HF_TOKEN=hf_...
export GHCR_TOKEN=ghp_...
export GHCR_USER=your-github-username
export CODEX_AUTH_FILE=/absolute/path/to/your/codex/auth.json
```

For an existing Modal login, keep its current `MODAL_CONFIG_PATH` instead. Use a subscription login JSON, not an API-key-only file. Do not paste its contents into prompts or logs.

Run the Python blocks below **in order in one Python session**, or combine them into a local Python file. They launch billed VMs and model requests. If a block fails, save its logs and jump to [cleanup](#8-download-evidence-and-clean-up).

## 2. Load one case on your machine

The full dataset row stays on your machine. Only the base repo/commit and scoped task are sent into prediction.

```python
import json
import os
import time
from pathlib import Path

import modal
import yaml
from huggingface_hub import HfApi, hf_hub_download

case_id = "sweeper-003"
revision = HfApi().dataset_info(
    "EVIGBYEN/SWEeper-Bench", revision="main", token=os.environ["HF_TOKEN"]
).sha
path = hf_hub_download(
    "EVIGBYEN/SWEeper-Bench", "data/test.jsonl",
    repo_type="dataset", revision=revision, token=os.environ["HF_TOKEN"],
)
rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
row = next(item for item in rows if item["case_id"] == case_id)
out = Path.cwd() / f"manual-{case_id}-{time.time_ns()}"
out.mkdir()
(out / "dataset-revision.txt").write_text(revision + "\n")
auth = Path(os.environ["CODEX_AUTH_FILE"]).read_text()
assert json.loads(auth).get("tokens", {}).get("access_token")
```

For a repeat run, replace `revision="main"` with a previously recorded SHA. Image tags can move independently of the dataset; the next step records their resolved digests.

## 3. Create the VM and pull images

There are three kinds of image: a small Ubuntu VM image that runs Docker, a product image containing the app's dependencies/startup tools, and an agent image containing Codex. Prediction and evaluation use different product images.

```python
app = modal.App.lookup("sweeper-manual", create_if_missing=True)
vm_image = (
    modal.Image.from_registry("ubuntu:24.04")
    .env({"DEBIAN_FRONTEND": "noninteractive"})
    .apt_install("docker.io", "git", "curl", "ca-certificates", "python3")
)
sb = modal.Sandbox.create(
    "sleep", "infinity",
    app=app, image=vm_image, cpu=8, memory=16384, timeout=14400,
    experimental_options={"vm_runtime": True},
    env={
        "GHCR_TOKEN": os.environ["GHCR_TOKEN"],
        "GHCR_USER": os.environ["GHCR_USER"],
        "CASE_ID": case_id,
        "REPO_URL": row["repo"],
        "BASE_COMMIT": row["base_commit"],
    },
)
(out / "sandbox-id.txt").write_text(sb.object_id + "\n")
print("Modal VM:", sb.object_id)

p = sb.exec("bash", "-lc", r'''
set -euo pipefail
mkdir -p /manual/evidence /manual/secrets /etc/docker
chmod 700 /manual/secrets
printf '%s\n' '{"default-ulimits":{"nofile":{"Name":"nofile","Hard":1048576,"Soft":1048576}}}' > /etc/docker/daemon.json
ulimit -n 1048576 || true
nohup dockerd > /manual/evidence/dockerd.log 2>&1 &
for i in $(seq 1 180); do docker info >/dev/null 2>&1 && break; sleep 1; done
docker info >/dev/null
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
for image in "$CASE_ID-prediction" "$CASE_ID" prediction-codex evaluation-codex; do
  docker pull "ghcr.io/evigbyen/$image:alpha"
  docker image inspect --format '{{json .RepoDigests}}' "ghcr.io/evigbyen/$image:alpha" >> /manual/evidence/image-digests.txt
done
''', timeout=1800)
bootstrap_log = p.stdout.read() + p.stderr.read()
p.wait()
(out / "bootstrap.log").write_text(bootstrap_log)
assert p.returncode == 0, "Inspect bootstrap.log before continuing"

sb.filesystem.write_text(auth, "/manual/secrets/auth.json")
p = sb.exec("chmod", "600", "/manual/secrets/auth.json")
p.wait()
assert p.returncode == 0
```

This builds only the VM bootstrap image. The four GHCR images are pulled as published; no product or agent Dockerfile is built here. `/manual/secrets` is not mounted into the product or included in downloaded evidence.

## 4. Check out the base and start the product

```python
p = sb.exec("bash", "-lc", r'''
set -euo pipefail
mkdir -p /manual/product
cd /manual/product
git init
git remote add origin "$REPO_URL"
git fetch --depth 1 origin "$BASE_COMMIT"
git checkout --detach FETCH_HEAD
test "$(git rev-parse HEAD)" = "$BASE_COMMIT"
git remote remove origin

docker run -d --name manual-product \
  -p 127.0.0.1:13200:3000 \
  -v /manual/product:/workspace/product \
  "ghcr.io/evigbyen/$CASE_ID-prediction:alpha"
docker exec -d manual-product bash -lc \
  'cd /workspace/product && HOST=0.0.0.0 PORT=3000 BROWSER=none product-yarn start >/tmp/product-dev-server.log 2>&1'
ready=0
for i in $(seq 1 120); do
  status=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:13200 || true)
  if [[ "$status" =~ ^[234][0-9][0-9]$ ]]; then ready=1; break; fi
  sleep 5
done
if [ "$ready" != 1 ]; then
  docker logs manual-product
  docker exec manual-product tail -n 100 /tmp/product-dev-server.log || true
  exit 1
fi
''', timeout=1500)
startup_log = p.stdout.read() + p.stderr.read()
p.wait()
(out / "prediction-startup.log").write_text(startup_log)
assert p.returncode == 0, "Inspect prediction-startup.log"
```

`product-yarn` is the startup interface shipped **inside the published product image**, not a helper script from this repo. It handles that image's application setup. HTTP readiness only confirms the app answers; the coding agent still needs to test the UI.

## 5. Invoke the coding agent directly

The prompt below uses the scoped task. It replaces the benchmark's task-shell instruction with an explicit `docker exec` command because this demonstration does not install that helper.

```python
prompt = f'''You are working in /workspace/product.

Please perform the following task:
{row["task_scoped"]}

Use the browser for end-to-end testing. Chromium and Playwright are preinstalled
in the product image. Run product commands and browser checks using:
  docker exec -w /workspace/product manual-product bash -lc '<command>'
Inside that product container the app listens on http://127.0.0.1:3000.
Use the product-* wrappers for package/runtime commands, as provided by the image.
Edit source in /workspace/product, shared with the product container.
Use only this checkout and these instructions. Do not retrieve external code,
known fixes, or Git history. Do not add remotes or commit.
Leave a reviewable source diff and explain your changes and checks.
'''
sb.filesystem.write_text(prompt, "/manual/task.md")
(out / "prediction-prompt.txt").write_text(prompt)

p = sb.exec("bash", "-lc", r'''
set -euo pipefail
docker run -d --name manual-coder --network host --entrypoint sleep \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /manual/product:/workspace/product \
  -v /manual/task.md:/manual/task.md:ro \
  -v /manual/evidence:/evidence \
  -v /manual/secrets/auth.json:/login/auth.json:ro \
  ghcr.io/evigbyen/prediction-codex:alpha infinity

docker exec manual-coder bash -lc '
  set -euo pipefail
  export CODEX_HOME=/opt/manual-codex
  mkdir -p "$CODEX_HOME"
  cp /login/auth.json "$CODEX_HOME/auth.json"
  chmod 600 "$CODEX_HOME/auth.json"
  unset OPENAI_API_KEY CODEX_API_KEY
  git config --global --add safe.directory /workspace/product
  cd /workspace/product
  codex exec --ignore-user-config --skip-git-repo-check \
    --dangerously-bypass-approvals-and-sandbox \
    --model gpt-6-astra \
    --config '\''model_reasoning_effort="xhigh"'\'' \
    --config '\''web_search="disabled"'\'' \
    --config features.apps=false --config features.multi_agent=false \
    --json --output-last-message /evidence/prediction-final.txt \
    - </manual/task.md >/evidence/prediction-trajectory.jsonl 2>/evidence/prediction-stderr.log
'
''', timeout=10800)
p.wait()
print("Coding agent exit:", p.returncode)
assert p.returncode == 0, "Download prediction-stderr.log before continuing"
```

The Docker socket gives this manual coding agent control over containers on this VM. Only the scoped prompt is mounted as its task, but this is a trusted-operator setup, not a hardened benchmark boundary. Stop the coding container before uploading any workflows.

## 6. Collect the patch and start a fresh evaluation app

Inspect the source changes, including untracked files. This demonstration collects `git diff --binary` only, so new untracked files stay out. The runner also intent-adds eligible new source files before that diff. Review `source-status.txt` before assuming the patch contains everything the agent intended.

```python
p = sb.exec("bash", "-lc", r'''
set -euo pipefail
docker rm -f manual-coder
cd /manual/product
git status --short > /manual/evidence/source-status.txt
git diff --binary > /manual/evidence/prediction-scoped.patch
test -s /manual/evidence/prediction-scoped.patch
docker rm -f manual-product
# Reset only this disposable checkout; preserve evidence in its separate directory.
git reset --hard "$BASE_COMMIT"
git clean -fdx
git apply /manual/evidence/prediction-scoped.patch

docker run -d --name manual-eval-product \
  -p 127.0.0.1:13200:3000 \
  -v /manual/product:/workspace/product \
  "ghcr.io/evigbyen/$CASE_ID:alpha"
docker exec -d manual-eval-product bash -lc \
  'cd /workspace/product && HOST=0.0.0.0 PORT=3000 BROWSER=none product-yarn start >/tmp/product-dev-server.log 2>&1'
ready=0
for i in $(seq 1 120); do
  status=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:13200 || true)
  if [[ "$status" =~ ^[234][0-9][0-9]$ ]]; then ready=1; break; fi
  sleep 5
done
if [ "$ready" != 1 ]; then
  docker logs manual-eval-product
  docker exec manual-eval-product tail -n 100 /tmp/product-dev-server.log || true
  exit 1
fi
''', timeout=1500)
eval_startup = p.stdout.read() + p.stderr.read()
p.wait()
(out / "evaluation-startup.log").write_text(eval_startup)
assert p.returncode == 0, "Inspect evaluation-startup.log"
```

For an unchanged baseline, omit `git apply`. For reference verification, upload a separately supplied reference patch and apply it instead. Use a fresh product container/checkout for each comparison.

## 7. Run a verifier directly and record the browser

This creates a browser container with no source or Docker socket mounted. A small inline Playwright program owns Chromium so its video is finalized after the agent finishes. Codex connects to that existing browser over CDP. Nothing calls the repo's browser runner or verifier adapter.

```python
p = sb.exec("bash", "-lc", r'''
set -euo pipefail
docker run -d --name manual-verifier --network host --shm-size=1g \
  --entrypoint sleep \
  -v /manual/evidence/verification:/evidence \
  -v /manual/secrets/auth.json:/login/auth.json:ro \
  ghcr.io/evigbyen/evaluation-codex:alpha infinity
docker exec manual-verifier bash -lc '
  mkdir -p /opt/manual-codex /manual-work
  cp /login/auth.json /opt/manual-codex/auth.json
  chmod 600 /opt/manual-codex/auth.json
'
''')
p.wait()
assert p.returncode == 0

workflows = yaml.safe_load(row["workflows"])["workflows"]
for index, workflow in enumerate(workflows):
    label = f"{index + 1:02d}-{workflow['category']}"
    folder = f"/evidence/{label}"
    # The published browser image supplies python3, Playwright, and Chromium.
    browser_code = f'''
import json, time
from pathlib import Path
from playwright.sync_api import sync_playwright
folder = Path({folder!r})
folder.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path="/usr/bin/chromium", headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--remote-debugging-port=9222"],
    )
    context = browser.new_context(
        record_video_dir=str(folder / "recordings"),
        viewport={{"width": 1920, "height": 1200}},
    )
    page = context.new_page()
    page.goto("http://127.0.0.1:13200", wait_until="domcontentloaded", timeout=120000)
    for item in {workflow.get("setup", [])!r}:
        if item["type"] == "clear_localStorage":
            page.evaluate("k => localStorage.removeItem(k)", item["key"])
        elif item["type"] == "set_localStorage":
            page.evaluate("x => localStorage.setItem(x.key, JSON.stringify(x.value))", item)
        else:
            raise ValueError("Unsupported setup: " + item["type"])
    (folder / "ready").touch()
    deadline = time.monotonic() + 2100
    while not (folder / "stop").exists() and time.monotonic() < deadline:
        page.wait_for_timeout(1000)
    context.close()  # Flush the recorded video.
    browser.close()
    (folder / "closed").touch()
'''
    # Start the browser owner process in the background, with its own log.
    import shlex
    browser_command = (
        f"mkdir -p {shlex.quote(folder)}; "
        f"python3 -u -c {shlex.quote(browser_code)} "
        f">{shlex.quote(folder + '/browser.log')} 2>&1"
    )
    p = sb.exec("docker", "exec", "-d", "manual-verifier", "bash", "-lc", browser_command)
    p.wait()
    assert p.returncode == 0
    p = sb.exec("docker", "exec", "manual-verifier", "bash", "-lc",
        f"for i in $(seq 1 150); do test -f {folder}/ready && exit 0; sleep 1; done; exit 1",
        timeout=180)
    p.wait()
    assert p.returncode == 0, "Browser did not become ready; inspect browser.log"

    task = (
        "Verify this workflow using only browser interactions. Do not modify the app.\n"
        "Use Python Playwright to connect_over_cdp('http://127.0.0.1:9222').\n"
        "Use the existing context and page; do not close them or launch another browser.\n"
        "App URL: http://127.0.0.1:13200. Do not use external websites, source, or DB shortcuts.\n"
        "Return JSON with result (pass/fail/uncertain) and observations for each step.\n"
        "Use fail for observed incorrect app behavior; uncertain for inability to reliably verify.\n"
        + json.dumps(workflow, ensure_ascii=False)
    )
    remote_task = f"/manual/evidence/verification/{label}/task.txt"
    sb.filesystem.write_text(task, remote_task)
    command = f'''
set -euo pipefail
export CODEX_HOME=/opt/manual-codex
unset OPENAI_API_KEY CODEX_API_KEY
cd /manual-work
/opt/agent-bin/codex exec --ignore-user-config --skip-git-repo-check \\
  --dangerously-bypass-approvals-and-sandbox --model gpt-5.6-luna \\
  --config 'model_reasoning_effort="xhigh"' --config 'web_search="disabled"' \\
  --config features.apps=false --config features.multi_agent=false \\
  --json --output-last-message {folder}/verdict.json \\
  - <{folder}/task.txt >{folder}/trajectory.jsonl 2>{folder}/stderr.log
'''
    p = sb.exec("docker", "exec", "manual-verifier", "bash", "-lc", command, timeout=1800)
    try:
        p.wait()
        print(label, "verifier exit:", p.returncode)
    finally:
        stop = sb.exec("docker", "exec", "manual-verifier", "touch", folder + "/stop")
        stop.wait()
        flush = sb.exec("docker", "exec", "manual-verifier", "bash", "-lc",
            f"for i in $(seq 1 60); do test -f {folder}/closed && exit 0; sleep 1; done; exit 1",
            timeout=90)
        flush.wait()
    assert flush.returncode == 0, "Video did not finish; inspect browser.log"
    assert p.returncode == 0, "Verifier failed; inspect stderr.log"
```

Each workflow gets its own recorded context. The app's server-side data persists between target and preservation, as it does within a benchmark phase. Inspect the returned JSON yourself; this manual example does not apply the repo's JSON parser, completeness rules, or automatic retries.

## 8. Download evidence and clean up

Run this even after an earlier failure. It downloads only the evidence directory, never the login file. Keep the original archive and inspect individual logs/videos locally.

```python
try:
    p = sb.exec("tar", "-czf", "/manual-evidence.tgz", "-C", "/manual/evidence", ".")
    p.wait()
    assert p.returncode == 0
    (out / "evidence.tgz").write_bytes(sb.filesystem.read_bytes("/manual-evidence.tgz"))
    print("Evidence saved in", out)
finally:
    sb.terminate()
```

The VM has a four-hour maximum lifetime as a backstop. Explicitly terminate it when done; do not leave a billed VM running after abandoning an interactive session.

## Differences from the benchmark

- Commands are visible here; no repository helper scripts or Python modules are invoked. The only image-specific app entry point is the published `product-yarn start` command.
- Prediction runs with direct Docker access and no provider-only firewall. This walkthrough is unsuitable for measuring adversarial isolation or claiming a benchmark-equivalent score.
- A single VM is reused between stages, with a fresh product container and reset source. The benchmark creates a separate evaluation VM pool.
- The prompt explicitly uses `docker exec` instead of `/workspace/task-shell`; the verifier prompt is also shown inline. These are demonstration prompts, not byte-identical benchmark prompts.
- Browser recording is implemented directly with Playwright. There are no automatic retries, shard scheduling, resume behavior, or phase-result aggregation.
- The commands have been syntax-checked, but this standalone sequence has not been claimed as a newly completed live benchmark. Published images and model access must still work in your environment.

For the scored runner's exact behavior, see [Design](design.md); for the supported CLI procedure, see [Guide](guide.md).
