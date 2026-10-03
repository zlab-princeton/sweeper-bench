# Guide

[README](../README.md) · [Agents](agents.md) · [Design](design.md) · [Manual walkthrough](manual-walkthrough.md)

1. [Install](#1-install)
2. [Connect accounts](#2-connect-accounts)
3. [Run a case](#3-run-a-case)
4. [Read the results](#4-read-the-results)
5. [Configure](#5-configure)
6. [Run many cases, resume, recover](#6-run-many-cases-resume-recover)
7. [Development checks](#7-development-checks)

## 1. Install

Python 3.11+ and Git are required locally. Modal runs the apps and agents; local Docker and GPUs are unnecessary. Run commands from the repository root. `scripts/run.py` imports directly from `src/`; there is no package-install step.

```bash
export XDG_CACHE_HOME="$PWD/.cache"
export HF_HOME="$XDG_CACHE_HOME/huggingface"
export PIP_CACHE_DIR="$XDG_CACHE_HOME/pip"
export TMPDIR="$XDG_CACHE_HOME/tmp"
mkdir -p "$TMPDIR"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp configs/config.toml.example configs/config.toml
```

Use scratch storage on a cluster with home quotas. For a new Modal login, set `MODAL_CONFIG_PATH="$PWD/.cache/modal.toml"` before authentication. Existing logins can continue using their current config. [requirements.txt](../requirements.txt) is the current dependency source; old run-specific environment constraints do not describe this pipeline.

## 2. Connect accounts

| Service | Purpose | Configuration |
|---|---|---|
| Modal | Run VMs | `modal token new`; optional `[modal].profile` |
| Hugging Face | Download the dataset | Export `HF_TOKEN` |
| GHCR | Pull product and agent images | Export `GHCR_TOKEN` and `GHCR_USER` |
| Model provider | Prediction and verification | See [Agents](agents.md) |

The GitHub token needs package-read access to the configured owner's images. The default owner is `evigbyen`; your login username and the image owner need not match.

```bash
modal token new
read -rsp 'Hugging Face token: ' HF_TOKEN; echo
export HF_TOKEN
read -rp 'GitHub username: ' GHCR_USER
export GHCR_USER
read -rsp 'GitHub token with image access: ' GHCR_TOKEN; echo
export GHCR_TOKEN
```

The CLI explicitly requires `HF_TOKEN` and `GHCR_TOKEN`. Named Modal Secrets and the old `modal_secret`/`registry_secret` config fields are not used. Codex subscriptions can read a local `auth_file` or an `auth_env` variable. Keep tokens out of the config, because the runner saves that config in the run directory.

## 3. Run a case

Configure your model and credentials using the [README example](../README.md#quickstart) or [Agents](agents.md). The copied default template uses API credentials and is not automatically configured for your subscription.

```bash
python scripts/run.py --cases sweeper-003 --run-dir runs/first
```

| Command | Behavior |
|---|---|
| `run` (default) | Finish prediction for all selected cases, then evaluate |
| `predict` | Run coding agents and collect patches |
| `evaluate` | Apply existing patches and verify; optionally baseline/reference |
| `status` | Print saved stage summaries and current per-case statuses |

```bash
python scripts/run.py predict --cases sweeper-003 --run-dir runs/pred
python scripts/run.py evaluate --cases sweeper-003 --predictions runs/pred --run-dir runs/eval
python scripts/run.py status --run-dir runs/eval
```

`--config path/to/settings.toml` selects another file. `--cases ID ...`, `--cases-file PATH`, or `--all` is mandatory except for `status`. A cases file is whitespace-separated IDs and may contain `#` comments. Output paths are resolved from your working directory. A full `run` uses the same root for prediction and evaluation.

## 4. Read the results

```text
runs/<name>/
  config.json                    Effective configuration (no credential contents)
  dataset.json                   Resolved dataset revision, SHA-256, case count
  cases.json                     Selected case metadata, including hidden workflows
  prediction-summary.json        Written when the prediction stage finishes
  evaluation-summary.json        Written when the evaluation stage finishes
  tmp/shard-*/                    VM ID and bootstrap logs
  predictions/<id>/
    result.json                  Lifecycle status, not the bug verdict
    worker.stdout.log / worker.stderr.log
    artifacts.tgz
    artifacts/
      prediction-scoped.patch    Or prediction-specified.patch
      logs-scoped/               Harness output, metadata, shell log
  evaluations/<id>/
    result.json
    worker.stdout.log / worker.stderr.log
    artifacts.tgz
    artifacts/
      result.jsonl
      logs/prediction-scoped-result.json
      logs/codex/                Codex trajectories, final answers, native sessions
      recordings/                Videos, only when recording is enabled
```

Artifacts depend on the selected harness and how far a case got. A failed worker may leave only logs. `tmp/shard-*` is shared across stages, so save diagnostic copies before another stage overwrites its bootstrap files.

| Case status | Meaning |
|---|---|
| `running` | The worker is active, including image pulls and app startup |
| `completed` | A valid nonempty patch was collected, or required evaluation cells are complete |
| `incomplete` | Worker returned but required artifacts/cells failed completeness checks |
| `skipped` | Evaluation had no valid prediction patch for a requested prediction phase |
| `infrastructure_failed` | VM/worker/collection failed; inspect the underlying error |

A startup failure caused by a model's patch can also get `infrastructure_failed`; the label alone does not establish its cause. A workflow verdict of `fail` can still have case status `completed`. Exit code zero means every selected stage result is completed, **not** that every workflow passed.

For each phase, `artifacts/logs/<phase>-result.json` contains `target` and `preservation` objects with counts and individual workflows. For example:

```bash
cat runs/first/evaluations/sweeper-003/artifacts/logs/prediction-scoped-result.json
```

Report target and preservation separately. If you compute “both checks passed,” state the denominator and keep missing evaluations separate. Enable `evaluation.record = 1` before launching to request videos. Codex writes `trajectory.jsonl`, `last-message.txt`, `execution.json`, and `native-sessions.tgz` under its workflow logs. The current pipeline does not promise the old runner's Playwright trace ZIP for every attempt.

## 5. Configure

Use [configs/config.toml.example](../configs/config.toml.example) for all supported top-level sections.

| Section | Important settings |
|---|---|
| `dataset` | `repo`, `file`, `revision`, `reference_patches` |
| `modal` | VM `app`, `region`, `cpu`, `memory_mb`, `timeout_seconds`, optional `profile` |
| `images` | GHCR `registry`, `owner`, `user`, `tag` |
| `prediction` | `harness`, `provider`, `model`, `effort`, `level`, `prompt_templates`, `time_budget`, `accounts`, `egress`, timeout |
| `evaluation` | Harness/provider/model/effort/accounts, `phases`, `categories`, `record`, `drop_caches`, timeout |
| `accounts.<name>` | `harness`, `auth`, `concurrency`; subscription `auth_file` or `auth_env` |

`prediction.level = "scoped"` gives a broad area to investigate; `"specified"` gives the detailed bug description. `prediction.prompt_templates` selects the pred wrapper: `"non-browser"` (default) reads [task-templates-non-browser.md](../src/runtime/prompts/task-templates-non-browser.md); `"browser"` reads [task-templates-browser.md](../src/runtime/prompts/task-templates-browser.md). The verifier task is constructed in [browser.py](../src/runtime/browser.py), with Codex-specific instructions in its [adapter](../src/runtime/harnesses/evaluation/codex.py).

Evaluation phases:

- `prediction` expands to the configured prediction level (`prediction-scoped` by default).
- `prediction-scoped` and `prediction-specified` select explicitly named patch files.
- `baseline` evaluates the unchanged base commit.
- `reference` applies `<case_id>.patch` from `dataset.reference_patches` (default `data/patches`). Those files must be supplied separately; `reference_commit` alone does not produce the patch. An unavailable reference phase is omitted, and a reference-only run without a patch ends incomplete.

For baseline-only verification, set `evaluation.phases = ["baseline"]` and use `evaluate`. For baseline, reference, and prediction together, set all three phases and supply both patches. See [Design §4](design.md#4-evaluation).

`evaluation.categories` defaults to `target,preservation`. Set it to `target` or `preservation` to run only that check. `drop_caches` defaults to `1`. After each evaluation phase, the VM drops its page cache when it can. Set `0` to leave the cache.

CLI overrides exist for each stage's harness, model, effort, and account list. Provider, level, phases, recording, image settings, and timeouts are configured in TOML. Check `python scripts/run.py --help` for the exact flags.

For reproducibility, pin `dataset.revision` to a commit SHA; `dataset.json` records the resolved SHA even when using `main`. Also retain the runner commit, config, prompt, and image digests from pull logs. An image tag such as `alpha` can move, and identical model outcomes are not guaranteed.

## 6. Run many cases, resume, recover

```bash
python scripts/run.py --cases sweeper-003 sweeper-040 sweeper-054 --run-dir runs/batch
python scripts/run.py --cases-file cases.txt --run-dir runs/batch
python scripts/run.py --all --run-dir runs/all-cases
python scripts/run.py --cases sweeper-003 sweeper-040 sweeper-054 --run-dir runs/batch --resume
```

A VM processes its assigned cases serially. Prediction and evaluation have separate VM pools; evaluation starts after the prediction stage finishes. Account concurrency contributes to the requested VM count, but current shard assignment is round-robin by account name. Use equal per-account concurrency values for balanced limits; unequal values are not enforced as individual account caps. Multiple independent CLI processes do not share a concurrency limit. See [Agents §4](agents.md#4-mixing-agents-and-accounts).

`--resume` skips complete artifacts. It rejects changes to stage harness, model, effort, or account names. It does not check every other input, so keep the dataset/image/prompt settings fixed yourself. Without resume, an existing prediction run directory is rejected. Evaluation can reuse a directory and loads prior outputs; use a fresh evaluation directory for an independent rerun that preserves the old result.

Retries cover verifier execution/JSON failures and agent uncertainty, as described in [Design §5](design.md#5-retries). They do not automatically repair images, failed app startup, or malformed patches. `skip_failed_tasks = 1` continues to later cases in a shard; it is not a retry switch.

Inspect `worker.stderr.log`, `worker.stdout.log`, and `tmp/shard-*/boot.*.log` when a case fails. Examples observed with this pipeline include missing image dependency directories, a failed startup leaving the app port occupied for the next case, and a patched app failing compilation. Record the evidence before assigning the cause to the image or the agent.

## 7. Development checks

For documentation/config edits, check Markdown links, TOML parsing, and `python scripts/run.py --help`. `git diff --check` catches whitespace errors. There is no longer a `scripts/smoke.py` or the previous packaged `src/sweeperbench` layout; historical scripts importing that package need migration.

For runtime changes, exercise the relevant credential, container, or browser path before running a large batch. A real Modal smoke run is billed and needs service access. Do not edit runtime files while Modal is building a bootstrap image from them: changing build inputs mid-upload can abort the build.
