# SWEeper-Bench

SWEeper-Bench is a benchmark of 200 real bugs in 200 interactive web applications. Each task asks a coding agent to find and fix problems in a named product area. The bug shows up only after a sequence of ordinary user actions, so the agent has to explore the running application. This repository is the runner: it loads a case, runs prediction, then checks the patch in a browser.

The dataset and the GHCR product images are not in this repository. Reference patches are not included either.

## How a case runs

1. **Load the case.** The runner downloads one task from the Hugging Face dataset named in `[dataset].repo`: repository URL, `base_commit`, `task_scoped`, `task_specified`, and `workflows`. `workflows` is one YAML string with a target test and a preservation test. Product images are pulled from GHCR under `[images].owner`: `sweeper-NNN-prediction:<tag>` for prediction and `sweeper-NNN:<tag>` for evaluation.
2. **Start a Modal VM.** Each VM uses `vm_runtime` and runs Docker. `concurrency` is how many VMs an account opens. Cases on one VM run one after another. The VM image contains `src/runtime/` only.
3. **Prediction.** The VM shallow-clones `base_commit` and starts the app on the VM loopback. A second container runs Codex, Claude Code, Cursor, Kimi, Gemini, DeepSeek, or Muse. The default prompt is `task_scoped` plus the non-browser template. The agent sees `/workspace/product` and does not receive `task_specified`, the workflows, the reference patch, or `reference_commit`. Product commands go through `/workspace/task-shell`. With the browser template, the agent's Playwright reaches the app through a localhost forward. Prediction egress is limited to the selected provider.
4. **Collect the patch.** Tracked edits, and new source files that are not runtime junk, become `prediction-scoped.patch` at the default level. The case workspace is then deleted. The worker frees the product port before the next case.
5. **Evaluation.** A later VM starts a clean app, applies the model patch, and runs Codex or browser-use with `--network host`. The verifier follows the two workflows:
   - **target** — the interactions that expose the bug, and whether the intended behavior is back.
   - **preservation** — whether nearby behavior still works.
6. **Record the verdict.** Each workflow is `pass`, `fail`, or `uncertain`. The runner stores the two results separately and does not write a combined score. Patches, logs, and optional browser recordings go to the run directory.

The default evaluation phase is `prediction`, expanded to `prediction-scoped`. `baseline` and `reference` are optional. A reference phase reads `data/patches/<case_id>.patch` on the host. A missing reference patch skips that phase.

`prediction.prompt_templates` selects the wrapper:

- `non-browser` (default) — [task-templates-non-browser.md](src/runtime/prompts/task-templates-non-browser.md)
- `browser` — [task-templates-browser.md](src/runtime/prompts/task-templates-browser.md), which also asks the agent to test the running app in the browser

`prediction.level = "specified"` gives the agent `task_specified` instead of `task_scoped`.

`HF_TOKEN`, `GHCR_TOKEN`, and the provider key stay in the host environment and are passed into the VM. Do not write them into `configs/config.toml`. Phases, retries, and isolation are in [Design](docs/design.md).

## Quickstart

The template runs **Codex on the official OpenAI API**: prediction `gpt-6-astra`, evaluation `gpt-5.6-luna`, `prompt_templates = "non-browser"`. Modal, GHCR pulls, and model calls are billed to you.

**1. Install.** Python 3.11+ and Git. The runner is not installed as a package.

```bash
git clone <this-repo> && cd <this-repo>
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

**2. Connect Modal, Hugging Face, and GHCR.** This repository pulls images. It does not build them.

```bash
modal token new
export HF_TOKEN=hf_...
export GHCR_TOKEN=ghp_...          # read:packages on the image owner
export GHCR_USER=...               # same value as [images].user
export OPENAI_API_KEY=...
```

**3. Configure.** Copy the template. Account blocks name the harness and auth mode. Stage sections select the account, model, and effort.

```bash
cp configs/config.toml.example configs/config.toml
```

```toml
[prediction]
harness = "codex"
provider = "openai"
model = "gpt-6-astra"
effort = "xhigh"
level = "scoped"
prompt_templates = "non-browser"
accounts = ["prediction"]

[evaluation]
harness = "codex"
provider = "openai"
model = "gpt-5.6-luna"
effort = "xhigh"
phases = ["prediction"]
accounts = ["evaluation"]

[accounts.prediction]
harness = "codex"
auth = "api"
concurrency = 5

[accounts.evaluation]
harness = "codex"
auth = "api"
concurrency = 5
```

Set `prompt_templates` to `browser` to add browser testing to the prediction prompt. Other harnesses are commented in `configs/config.toml.example`. For browser-use evaluation, set `evaluation.harness = "browser-use"` and use the same harness on the evaluation account. See [Agents](docs/agents.md).

A Codex subscription uses a local login file. Put the path in the account block, not the file contents:

```toml
[accounts.codex]
harness = "codex"
auth = "subscription"
auth_file = "/absolute/path/to/your/codex/auth.json"
concurrency = 1
```

Point `prediction.accounts` or `evaluation.accounts` at `["codex"]`. The runner reads that file, or `CODEX_AUTH_JSON`, and passes it into the VM.

**4. Run.** `--cases`, `--cases-file`, or `--all` is required. A cases file is whitespace-separated IDs and may contain `#` comments. Omitted flags come from `configs/config.toml`.

```bash
python scripts/run.py --cases sweeper-001 --run-dir runs/first
```

| Command | Does |
|---|---|
| `run` (default) | `predict`, then `evaluate` |
| `predict` | Start the app, run the coding agent, collect the patch |
| `evaluate` | Apply a patch on a fresh app and run the browser verifier |
| `status` | Print summaries for a run directory |

```bash
python scripts/run.py predict  --cases sweeper-001 --run-dir runs/first
python scripts/run.py evaluate --cases sweeper-001 --predictions runs/first --run-dir runs/eval-2
python scripts/run.py status   --run-dir runs/first
```

A prediction directory is single-use unless you pass `--resume`. Resume keeps finished results and adds new case rows to `cases.json`. If a VM dies, the host recovers statuses already written under `predictions/` or `evaluations/`. `evaluate` may write into an existing directory. The exit code is `0` only when every selected case is `completed`.

**5. Read the verdict.** Case `status` is `completed`, `incomplete`, `skipped`, or `infrastructure_failed`. A completed prediction means a nonempty source patch was collected. Workflow verdicts are in the evaluation artifacts.

```bash
python scripts/run.py status --run-dir runs/first
cat runs/first/evaluations/sweeper-001/result.json
cat runs/first/evaluations/sweeper-001/artifacts/result.jsonl
cat runs/first/evaluations/sweeper-001/artifacts/logs/prediction-scoped-result.json
```

### Timed prediction

`prediction.time_budget` is `"unlimited"` in the template, so the agent has no deadline. A positive number of minutes applies to Codex and Claude Code only. The runner then appends [time-budget.md](src/runtime/prompts/time-budget.md). The deadline starts at the first agent call, after the app is up. An early successful exit resumes the same session until the deadline. Failures and quota errors stop. At the budget plus 5 minutes, a still-running agent is stopped and asked to submit. That submission turn lasts 5 minutes. Set the prediction case `timeout_seconds` above the budget plus 10 minutes, with room for startup. Timing is stored in `timed-run.json`.

## Docs

- [Guide](docs/guide.md) — install, run, read results, resume
- [Agents](docs/agents.md) — harnesses, models, accounts
- [Design](docs/design.md) — pipeline, retries, isolation
- [Manual walkthrough](docs/manual-walkthrough.md) — images and Codex directly on Modal, outside this runner

## Layout

```
scripts/run.py                CLI
configs/config.toml.example   Template. Copy to configs/config.toml
src/                          Host runner
src/runtime/                  Copied into each VM
docs/                         Guide, agents, design, walkthrough
```

A run writes `runs/<name>/` with `config.json`, `dataset.json`, `cases.json`, summaries, `predictions/<case>/artifacts/prediction-scoped.patch`, and `evaluations/<case>/artifacts/result.jsonl`. Optional reference patches live in `data/patches/<case_id>.patch`.

## Citation

```bibtex
@misc{sweeperbench2026,
  title        = {SWEeper-Bench: Can Agents Discover Bugs in Interactive Software?},
  author       = {Yang Yao and Haozhe Chen and Bingyi Kang and Karthik R Narasimhan and Zhuang Liu},
  year         = {2026},
  note         = {Preprint. Yang Yao and Haozhe Chen contributed equally.}
}
```
