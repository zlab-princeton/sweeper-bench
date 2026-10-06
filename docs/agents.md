# Agents

[README](../README.md) · [Guide](guide.md) · [Design](design.md) · [Manual walkthrough](manual-walkthrough.md)

1. [How agents are configured](#1-how-agents-are-configured)
2. [Codex](#2-codex)
3. [Other harnesses](#3-other-harnesses)
4. [Mixing agents and accounts](#4-mixing-agents-and-accounts)
5. [Claude Code subscriptions](#5-claude-code-subscriptions)

## 1. How agents are configured

A stage selects the harness, provider, model, effort, and account names. An account selects the authentication mode and contributes a concurrency setting. Every account selected by a stage must have that stage's harness.

```toml
[prediction]
harness = "codex"
provider = "openai"
model = "gpt-6-astra"
effort = "xhigh"
level = "scoped"
accounts = ["my-codex"]

[accounts.my-codex]
harness = "codex"
auth = "subscription"
auth_file = "/absolute/path/to/codex/auth.json"
concurrency = 1
```

This is a configuration fragment; retain the other sections from the [full template](../configs/config.toml.example). The [README](../README.md#-quickstart) and the template use API keys; this fragment shows a subscription account instead.

Credentials are read on the host and passed into the Modal VM. Subscription contents are not stored in saved configs. Config paths and environment-variable names are safe to store, but credential values are not.

The VM pulls prebuilt harness images from `[images].registry/owner` with the configured tag:

- Prediction: `prediction-<harness>:<tag>`.
- Evaluation: `evaluation-codex:<tag>` or `evaluation-browser-use:<tag>`.

Changing a harness requires access to its published image and model provider. The local runner does not build those images or install their agent CLIs.

## 2. Codex

### Subscription authentication

Use `provider = "openai"` in both stages and `auth = "subscription"` in the account. Point `auth_file` at the login JSON created by your Codex CLI. It must contain subscription tokens, including an access token; an API-key-only auth file is not a substitute.

Alternatively, omit `auth_file` and select an environment variable:

```toml
[accounts.my-codex]
harness = "codex"
auth = "subscription"
auth_env = "MY_CODEX_AUTH_JSON"
concurrency = 1
```

```bash
export MY_CODEX_AUTH_JSON="$(cat /absolute/path/to/codex/auth.json)"
```

If neither selector is set, the default variable is `CODEX_AUTH_JSON`. `auth_file` takes precedence. Each VM receives the selected account's JSON. Prediction writes a private `auth.json` into its Codex home; evaluation mounts a credential file and copies it into the verifier's Codex home. Subscription mode ignores ambient OpenAI API credentials. VM/container copies are disposable; refreshed login state is not synchronized back to your host file.

### API authentication

Codex API mode is official OpenAI only: `provider = "openai"`, with `OPENAI_API_KEY` or `CODEX_API_KEY`. The endpoint is `https://api.openai.com/v1`. Evaluation Codex uses the same provider. Subscription mode is separate and does not use an API key.

### What Codex receives

Prediction receives the selected task inserted into the configured prompt template. `prediction.prompt_templates = "browser"` (default) uses [task-templates-browser.md](../src/runtime/prompts/task-templates-browser.md); `"non-browser"` uses [task-templates-non-browser.md](../src/runtime/prompts/task-templates-non-browser.md). It edits `/workspace/product` and uses `/workspace/task-shell` for product commands. The wrapper executes those commands in the product container and logs them.

The prediction command selects the model and `model_reasoning_effort`, disables web search, apps, and multi-agent use, and runs without Codex's own approval/sandbox boundary inside the Docker setup. The network filter and container setup provide the outer restrictions; see [Design §6](design.md#6-isolation-what-is-enforced-what-is-not).

Evaluation receives a workflow and instructions to connect Playwright to an existing recorded Chromium context. It must use the UI and avoid source/database/API shortcuts unless the workflow explicitly permits them. Its [adapter](../src/runtime/harnesses/evaluation/codex.py) records the command, trajectory, final JSON, and native sessions. The adapter's default individual Codex timeout is 1,800 seconds; `[evaluation].timeout_seconds` limits the entire case worker, not each Codex invocation. The browser-use step limit is not a Codex turn limit.

## 3. Other harnesses

Prediction also supports Claude Code, Cursor, Kimi Code, Gemini CLI, DeepSeek, and Muse Code. Set `prediction.harness`, `provider`, `model`, and the matching account together. The alternatives are listed in the comments of [config.toml.example](../configs/config.toml.example).

Each harness uses its official API. Claude Code uses `provider = "anthropic"` and `ANTHROPIC_API_KEY`. Kimi Code uses `provider = "kimi"` and `KIMI_API_KEY` or `MOONSHOT_API_KEY` against `api.moonshot.cn`. Gemini CLI uses `provider = "gemini"` or `"google"` and `GEMINI_API_KEY`. DeepSeek uses `provider = "deepseek"` and `DEEPSEEK_API_KEY` against `api.deepseek.com`. Muse Code uses `provider = "meta"` and `META_API_KEY` or `MODEL_API_KEY` against `api.meta.ai`. Cursor uses `CURSOR_API_KEY`. Timed prediction is `[prediction].time_budget`; `"unlimited"` is the default, and a positive budget is valid only for Codex and Claude Code.

Evaluation supports **Codex** and **browser-use**. Codex evaluation is official OpenAI. browser-use accepts `openai` (`OPENAI_API_KEY`) or `anthropic` (`ANTHROPIC_API_KEY`), with no custom base URL.

Both evaluators use the workflow construction and browser lifecycle in [browser.py](../src/runtime/browser.py). Set `evaluation.record = 1` for recording; default templates set it to `0`. The `effort` setting is passed into Codex; the browser-use LLM constructor does not currently consume it.

## 4. Mixing agents and accounts

Different stages may choose different harnesses. For example, Claude Code prediction and Codex evaluation need separate account blocks with matching harnesses. The model and effort belong to the stage, so all accounts in a stage run the same model/effort.

For two Codex subscriptions:

```toml
[accounts.first]
harness = "codex"
auth = "subscription"
auth_file = "/absolute/path/to/first/auth.json"
concurrency = 4

[accounts.second]
harness = "codex"
auth = "subscription"
auth_file = "/absolute/path/to/second/auth.json"
concurrency = 4
```

Set `accounts = ["first", "second"]` in each desired stage. One VM runs a shard of cases serially, then shuts down. The stage requests up to the sum of concurrency values, but chunking can produce fewer VMs; for example, 20 cases with eight requested slots produces seven shards. Accounts are assigned to shards round-robin.

**Use equal concurrency values for equal account caps.** The current scheduler does not allocate shards proportionally to unequal per-account limits. Separate CLI processes also have independent limits, so do not assume four configured slots means four globally across every run.

Multiple subscription accounts have independent auth selectors. API accounts share the host environment unless an account sets `api_key_env`. That variable is copied into the VM under `api_key_name`, or under the same name when `api_key_name` is omitted. See [runner.py](../src/runner.py) and [modal_vm.py](../src/modal_vm.py) for the actual scheduling and credential mapping.

## 5. Claude Code subscriptions

Use `prediction.harness = "claude-code"`, `provider = "anthropic"`, and an
account with `harness = "claude-code"`, `auth = "subscription"`. Set its
`auth_file` to a private text file containing a Claude `setup-token` OAuth token,
or `auth_env` to the environment variable holding that token (default
`CLAUDE_CODE_OAUTH_TOKEN`). Do not put the token in TOML. The selected account's
token is passed at runtime; subscription mode ignores ambient Anthropic API keys.
Evaluation still uses a separate supported verifier account, such as Codex.

Headless prediction requires a Claude CLI supporting `--safe-mode` and `--effort`.
The harness fails if required isolation flags are unavailable. It uses a fresh
configuration directory, disables custom settings, memory, MCP, web tools and
subagents, and exposes only local file and shell tools. Browser testing through
shell commands remains available. Prediction must keep egress isolation enabled:
container networking blocks general internet access; the host proxy permits the
configured provider allowlist. The agent runs unprivileged and its task-shell
can target only the root-pinned product container. Scoped prediction receives
neither hidden workflows nor reference patches.

Artifacts include `trajectory.jsonl` (stream events, terminal result and token
usage), `claude-sessions.tgz` (native session JSONL files), `prompt.md`,
`claude-version.txt`, and `final.txt` on success. Failure diagnostics are collected
before workspace cleanup.

These controls restrict access; they do not prove that arbitrary case images
contain no clues or that agent-writable evidence is tamper-proof. Review native
traces and host network logs when auditing a run.
