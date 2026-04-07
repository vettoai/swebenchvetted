# SWE-bench Eval

Local evaluation tool for the SWE-bench Vetted dataset. Runs coding agents against task environments using Docker, scores their patches, and computes pass@k metrics.

## Prerequisites

- **Python >= 3.13**
- **[uv](https://docs.astral.sh/uv/)** (package manager)
- **Docker** (daemon must be running)
- An API key for the LLM provider you want to use (OpenAI, Anthropic, Google, etc.)

## Installation

```bash
cd swebenchvetted
uv sync
```

This installs the `swebench-eval` CLI into the project's virtual environment.

## Quick start

```bash
# Set your API key
export GEMINI_API_KEY="..."      # or OPENAI_API_KEY, ANTHROPIC_API_KEY

# Run evaluation on a single task file
uv run swebench-eval run tasks/pallets__flask.jsonl -m gemini/gemini-3.1-pro-preview

# Run on a directory of tasks with 4 parallel workers
uv run swebench-eval run tasks/ -m openai/gpt-4o -j 4

# View results from a previous run
uv run swebench-eval results results/run_20260330_014710
```

## Commands

### `run` — Run evaluation

```
uv run swebench-eval run <input> [options]
```

Builds Docker images, starts a LiteLLM proxy, runs the agent inside each task container, then scores the produced patches.

| Option | Short | Default | Description |
|---|---|---|---|
| `--model` | `-m` | `openai/gpt-4o` | LiteLLM model string (see [LiteLLM docs](https://docs.litellm.ai/docs/providers) for supported providers) |
| `--api-base` | | | Custom API endpoint (LiteLLM forwards to it) |
| `--api-key` | `-k` | | API key; also reads from `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` env vars |
| `--attempts` | `-n` | `3` | Number of independent attempts per task |
| `--concurrent` | `-j` | `4` | Maximum parallel task evaluations |
| `--timeout` | | `1800` | Agent timeout in seconds (per attempt) |
| `--output` | `-o` | `./results` | Output directory |
| `--force-rebuild` | | `false` | Rebuild Docker images even if cached locally |
| `--no-live` | | `false` | Disable live-updating display; use plain log output |
| `--filter` | | | Only run tasks whose ID starts with this prefix |
| `--stagger` | | `0` | Seconds to wait between launching each task (reduces LLM proxy contention) |

**Input** can be:

- A single `.json` file (JSON array, single object, or JSONL)
- A single `.jsonl` file
- A directory (all `*.json` and `*.jsonl` files are loaded)

### `build` — Build images only

```
uv run swebench-eval build <input> [options]
```

Builds Docker images for all tasks without running evaluation. Useful for warming the image cache before a full run.

| Option | Short | Default | Description |
|---|---|---|---|
| `--force-rebuild` | | `false` | Rebuild even if image exists locally |
| `--concurrent` | `-j` | `4` | Max parallel builds |
| `--filter` | | | Filter tasks by ID prefix |

### `results` — View results

```
uv run swebench-eval results <run_dir>
```

Displays the summary and per-task results table from a previous run directory.

## How it works

Each evaluation run goes through four phases:

1. **Build** — For each task, a Docker image is built from the task's Dockerfile and context files. Images are tagged `swebenchvetted-eval/<task_id>:latest` and cached locally.

2. **LiteLLM proxy** — A LiteLLM proxy starts on `localhost:10000`, forwarding requests to the configured model provider. The proxy is configured with:
   - **Multiple workers** (`--num_workers`) matching the `--concurrent` setting
   - **Rate limit retries** (10 retries with 5s backoff) to handle provider throttling
   - **Auto-restart** — if the proxy process dies mid-run, it is automatically restarted before the next attempt

   The agent inside the container talks to this proxy using `--network host`.

3. **Evaluate** — For each task, up to `n` attempts are run. When `--stagger` is set, task launches are spaced apart to reduce initial contention on the LLM proxy.
   - **Agent run**: Executes `/testbed/evaluate.sh` inside the task container with the problem statement mounted. The agent (deepagents) produces a patch.
   - **Scoring run**: Applies the agent's patch plus the test patch, runs `/testbed/verify_solution`, and checks results. Generated tests must pass, and no existing (non-ignored) tests may regress.

4. **Aggregate** — Results are collected and pass@1, pass@3, and resolved rate are computed using the unbiased estimator from the Codex paper (Chen et al., 2021).

## API keys

The LiteLLM proxy reads API keys from environment variables automatically. Set the one matching your provider:

```bash
export OPENAI_API_KEY="sk-..."
export ANTHROPIC_API_KEY="sk-ant-..."
export GEMINI_API_KEY="AI..."
```

Or pass it directly with `--api-key` (sets all three env vars as a convenience).

## Model strings

Models use LiteLLM's provider/model format:

| Provider | Example |
|---|---|
| OpenAI | `openai/gpt-4o`, `openai/o3` |
| Anthropic | `anthropic/claude-sonnet-4-6` |
| Google | `gemini/gemini-3.1-pro-preview` |

See the full list at https://docs.litellm.ai/docs/providers.

## Output structure

Results are written to `results/run_<timestamp>/`:

```
results/run_20260330_014710/
├── summary.json                              # ModelEvaluation (pass@k, all task results)
├── tasks/
│   └── pallets__flask__5928.json             # TaskEvaluation for this task
└── logs/
    └── pallets__flask__5928/
        ├── attempt_0_agent.log               # Agent stdout/stderr
        ├── attempt_0_scoring.log             # Scoring stdout/stderr
        ├── attempt_0_patch.diff              # Patch produced by the agent
        ├── attempt_1_agent.log
        ├── attempt_1_scoring.log
        ├── attempt_1_patch.diff
        ├── attempt_2_agent.log
        ├── attempt_2_scoring.log
        └── attempt_2_patch.diff
```

All JSON files use camelCase keys matching the cloud pipeline schema.

## Task JSON format

Each task follows this schema (camelCase):

```json
{
  "metadata": {
    "id": "pallets__flask__5928",
    "source": "https://github.com/pallets/flask/pull/5928",
    "language": "python"
  },
  "issue": {
    "description": "Teardown callbacks short-circuit on exceptions..."
  },
  "verifier": {
    "addedTests": [
      {
        "filePath": "tests/test_example.py",
        "testName": "test_something",
        "content": "import pytest\n..."
      }
    ],
    "deletedTests": [],
    "ignored": [
      "tests/test_example.py::test_known_flaky"
    ]
  },
  "environment": {
    "imageName": "...",
    "dockerfile": "FROM ubuntu:24.04\n...",
    "contextFiles": {
      "evaluate.sh": "#!/bin/bash\n...",
      "verify_solution": "#!/bin/bash\n..."
    }
  }
}
```

## Reliability features

Several mechanisms improve evaluation reliability across different models and providers:

- **Staggered starts** (`--stagger`): Spaces out task launches to avoid overwhelming the LLM proxy when many tasks start simultaneously.
- **LiteLLM multi-worker proxy**: The proxy spawns one uvicorn worker per concurrent task, preventing a single-worker bottleneck.
- **Rate limit retries**: The LiteLLM proxy retries rate-limited requests (HTTP 429) up to 10 times with exponential backoff before propagating the error.
- **Proxy auto-restart**: Before each attempt, the evaluator checks proxy liveness. If the proxy process has died, it is automatically restarted.
- **Early abort on proxy failure**: If the container's health check cannot reach the proxy within 120 seconds, the attempt exits immediately instead of wasting the full timeout.
- **Robust patch capture**: The `evaluate.sh` script saves the original `HEAD` before the agent runs and diffs against it afterward using `git add -A && git diff $ORIG_HEAD`. This captures all changes including new files and changes the agent may have committed via `git commit`.

## Examples

Run a single task with Gemini, 1 attempt:

```bash
uv run swebench-eval run task.json -m gemini/gemini-3.1-pro-preview -n 1
```

Run all tasks in a directory with OpenAI, 8 parallel workers, staggered 10s apart:

```bash
uv run swebench-eval run tasks/ -m openai/gpt-4o -j 8 -n 3 --stagger 10
```

Run only Django tasks:

```bash
uv run swebench-eval run tasks/ -m openai/gpt-4o --filter django__django
```

Pre-build images without running evaluation:

```bash
uv run swebench-eval build tasks/ -j 8
```

Plain log output (for CI or piping):

```bash
uv run swebench-eval run tasks/ -m openai/gpt-4o --no-live 2>eval.log
```
