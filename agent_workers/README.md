# Configuration and integration guide

[Project overview](../README.md) · [Configuration](#configuration) · [CLI](#command-line-interface) · [Python API](#python-integration) · [Docker](#platforms-and-docker) · [Русская инструкция](README.ru.md)

agent-workers runs one turn through a subscription CLI, using one named account directory. It manages login, process supervision, usage measurements, normalized replies, and a local outbox. Several accounts mean several workers with separate settings; there is no shared profile registry.

The package uses only the Python standard library. A live turn additionally requires Claude Code or Codex and a supported subscription account. For installation, see the [English quick start](../README.md#quick-start).

## Configuration

The package searches for `.env` in the current directory and up to four parent directories. Precedence is **command-line overrides → environment → `.env` → defaults**. The [example file](.env.example) is in English.

| Setting | Purpose | Default |
| --- | --- | --- |
| `AGENT_PROVIDER` | `claude` or `codex`. | Required. |
| `AGENT_HOME` | Provider account/credentials directory. | `~/.agent-worker/<provider>` |
| `AGENT_MODEL` | Model identifier understood by the provider CLI. | Adapter default. |
| `AGENT_EFFORT` | Reasoning effort for turns. | Claude: CLI default; Codex: `medium`. |
| `AGENT_RUNS` | Persistent run folders and logs. | `<AGENT_HOME>/runs` |
| `AGENT_REFUSE_ABOVE` | Refuse a new turn at this percentage of an applicable usage window. | `95`; empty disables the threshold. |
| `AGENT_SPEND_CREDITS` | Allow a turn when a window is exhausted but provider credits remain or are unlimited. | `1`; set `0` to refuse. |
| `AGENT_TIMEOUT` | Total turn timeout in seconds. | `3600`; empty disables. |
| `AGENT_IDLE_TIMEOUT` | Maximum time without CLI output, in seconds. | `900`; empty disables. |
| `AGENT_CLAUDE_BINARY` | Explicit path to the Claude Code executable. | Find on `PATH`. |
| `AGENT_CODEX_BINARY` | Explicit path to the Codex executable. | Find on `PATH`. |

Boolean values accept `1/true/yes` and `0/false/no` (also their supported Russian equivalents); invalid values fail configuration. Timeout values must be positive when supplied.

Relative paths in `.env` resolve from the directory containing that file. Paths supplied through environment variables or command flags resolve from the current working directory. This also applies to executable paths. Paths are converted to absolute paths when settings are loaded.

### Account directories

`AGENT_HOME` becomes `CLAUDE_CONFIG_DIR` or `CODEX_HOME` in the subprocess. Keep it outside your repository. It contains credentials and may contain session logs, prompts, and answers; files and directories are created with owner-only permissions on Linux.

To switch accounts, change `AGENT_HOME` and sign in to that directory. The previous directory retains its credentials. Two directories signed into the same account still share that account's subscription limits; independent windows require different accounts.

### Reasoning effort

Claude accepts `low`, `medium`, `high`, `xhigh`, and `max`. The adapter validates these when building the worker and passes effort only to turn commands, not login or usage probes. It also supplies an explicit settings override so `AGENT_EFFORT` takes precedence over the account's effort setting; organization-managed settings can still take precedence.

Codex receives the value as `model_reasoning_effort`. The model/CLI decides which values it supports. An empty effort setting means use the provider default.

### Limit policy

For Python callers, `LimitPolicy` also exposes:

| Field | Purpose |
| --- | --- |
| `before`, `after` | Enable measurements before/after a turn. |
| `refuse_above` | Refusal threshold; `None` disables it. |
| `spend_credits` | Allow provider credits when subscription windows are exhausted. |
| `settle_reads`, `settle_delay` | Re-read usage after the turn to allow delayed accounting to catch up. |
| `stale_after`, `on_unknown` | Handle old or unavailable measurements. |

Disabling measurements with `before=False, after=False` also disables limit-based refusal. Total/inactivity timeouts belong to `Worker` as `timeout` and `idle`; use `None` to disable either.

## Command-line interface

Run commands in the project where the package is installed:

```bash
uv run python -m agent_workers login
uv run python -m agent_workers status
uv run python -m agent_workers logout
uv run python -m agent_workers run "Reply with one word: ready"
uv run python -m agent_workers pending
uv run python -m agent_workers collect task-0042
uv run python -m agent_workers collect --all
```

`login` opens an interactive provider sign-in flow; choose subscription sign-in for the account. `status` checks login and reads usage without asking a model. `pending` and `collect` inspect stored run folders and work even when the provider CLI is unavailable.

Override connection settings per command:

```bash
uv run python -m agent_workers status --provider codex --home ../agent-accounts/codex
uv run python -m agent_workers run --provider codex --effort high "Summarize the request."
```

Commands print their connection details to stderr. A turn's reply goes to stdout, with usage and diagnostic information on stderr, so it can be redirected:

```bash
uv run python -m agent_workers run "Reply with one word: ready" > answer.txt
```

CLI help and diagnostic messages currently use Russian. The commands and flags above are unchanged across languages; the prompt controls the language requested from the model.

### Preview a turn

```bash
uv run python -m agent_workers run --dry-run "Reply with one word: ready"
```

A dry run prints the provider command and task-file contents without starting the provider process or checking login. It builds the request in a temporary directory, then deletes it. Paths printed in this preview are illustrative and no longer exist afterwards.

For a long prompt, use `-` to read stdin. Pass a system instruction with `--system-file`:

```bash
uv run python -m agent_workers run --system-file instructions.txt - < prompt.txt
```

Every manual `run` uses a new random key. Printing a successful reply counts as delivery, and its run folder is removed immediately. An interrupted turn retains its folder and prints its location and cleanup command. For keyed recovery, use the Python API.

## Python integration

### Build a worker

```python
from agent_workers import build

worker = build()
worker.check()  # Raises if subscription login cannot be confirmed.
```

Or supply settings explicitly:

```python
from agent_workers import Settings, build

worker = build(Settings().override(
    provider="codex",
    home="/data/account",
    model="gpt-5.6-luna",
))
```

To construct the underlying components directly:

```python
from pathlib import Path

from agent_workers.base import LimitPolicy, Profile, Worker
from agent_workers.providers import CodexAdapter

worker = Worker(
    CodexAdapter(model="gpt-5.6-luna", effort="low"),
    Profile("codex-worker", Path("/data/account")),
    Path("/data/runs"),
    LimitPolicy(refuse_above=80),
)
```

Model identifiers in these examples must be available to your CLI/account; change them to your deployment's supported model.

### Make a turn

```python
result = worker.run(
    {
        "user": "Identify the acceptance criteria.",
        "system": "Answer in English and keep each criterion testable.",
        "model": "gpt-5.6-luna",
        "workspace": "/repos/project",
    },
    key="task-0042",
    ensure_login=True,
)
```

Only `user` is required in the request. `system`, `model`, and `workspace` are optional. An absent `model` uses the adapter's model. Each turn starts a new conversation; this API does not continue sessions.

| Keyword argument | Behavior |
| --- | --- |
| `key` | Required task identifier, also used for the run folder. Do not derive it from prompt text. |
| `stop` | Callable returning whether to stop; supervision preserves captured output. |
| `ensure_login` | Check login only when a fresh turn is needed. A completed saved answer can be resumed without login. |

Without a workspace, the turn has no tools. With one, the provider runs in that directory with read-only inspection enabled: Claude uses `Read`, `Grep`, and `Glob`, with workspace-local settings/hooks excluded; Codex uses `-C` and a `read-only` sandbox. A nonexistent workspace is rejected before login checks and usage measurements.

Read-only access prevents edits; it does not isolate the filesystem. Claude and Codex may read outside the working directory. Use trusted code, or an OS-level isolated container with only the necessary mounts when inspecting untrusted material.

### Inspect the result

| Field | Contents |
| --- | --- |
| `state` | Outcome of the attempt; see below. |
| `reply` | `Reply` with `.text`, `.complete`, `.diagnostic`, `.model`, or `None`. |
| `tokens` | Normalized usage: `.input`, `.cached_input`, `.cache_write`, `.output`, `.reasoning`. |
| `cost` | Equivalent API price: `.amount`, `.currency`, `.source`, `.parts`, or `None`. |
| `before`, `after` | Subscription measurements, or `None`. |
| `spent` | Measured usage-window deltas, such as `{"session": 0.4}`. |
| `reason` | Reason the turn was refused or interrupted, when available. |
| `entry` | Run entry with `.folder` and `.meta`. Interruption metadata can be `stopped`, `timeout`, or `idle`. |

| State | Meaning | Caller action |
| --- | --- | --- |
| `answered` | A fresh turn returned a complete answer. | Accept/persist the result, then collect. |
| `resumed` | A complete answer was recovered from the outbox. No new turn. | Accept/persist it idempotently, then collect. |
| `incomplete` | CLI error, parsing failure, interruption, or timeout after launch. | Report the failure and diagnostics. Let the coordinator decide whether to retry. |
| `in_progress` | Another live process owns this key. | Leave the folder alone. |
| `limit_reached` | Usage policy refused the turn before launch. | Requeue according to the reset time in `before`; no run folder remains. |
| `aborted` | A stop request arrived before CLI launch. | Return/requeue the task; no paid turn started and no folder remains. |

A total timeout catches an active but unproductive CLI; the idle timeout catches a CLI that stops writing to its logs. Both are intentionally generous because a long reasoning step can be silent. A turn interrupted by `stop` skips the after-measurement so shutdown does not wait for additional probes and settlement delays.

Pre-launch failures raise exceptions and leave no run folder: invalid requests, `LoginRequired` (a `RuntimeError` subclass), and `LaunchError` (an `OSError` subclass). Failures after launch generally return `incomplete`, including output-parsing errors with `reply.diagnostic` starting with `reply_error:`. Keyboard interrupts still propagate while preserving an interrupted launched turn for inspection.

### Outbox and acknowledgement

```python
result = worker.run(request, key=job_id, ensure_login=True)
# Persist/deliver the result to your receiver here.
worker.collect(result["entry"])  # Only after the receiver accepts it.
```

The outbox retains a run until the caller collects it, with no automatic retention timer. A completed answer is recovered from the captured output, even if the process died before recording the final state.

A repeated key returns a saved complete answer before checking login or measuring limits. An incomplete attempt without a saved complete answer is started again when the coordinator retries that key; it can consume subscription allowance again. After collection, no saved result remains for that key.

`worker.pending()` lists unowned entries, including completed answers and interrupted attempts. `collect()` deletes a folder under its operating-system lock; it raises `Busy` when the entry is running and `NotRemoved` if cleanup fails. A failed cleanup means the result may appear again. Do not delete run folders manually.

Outbox inspection can be independent of the provider CLI:

```python
from pathlib import Path
from agent_workers.base.outbox import Outbox

outbox = Outbox(Path("/data/runs"))
for entry in outbox.pending():
    print(entry.folder.name, entry.outcome)
```

Locks apply on one machine. Do not use one run directory across multiple hosts over a network filesystem. Rely on the public result and entry metadata instead of parsing internal journal files, whose format follows the CLI versions.

### Coordinator responsibilities

Your runtime claims tasks, maintains leases, reports results, and schedules retries. Keep the lease alive for **all of `run()`**, including login checks and before/after measurements, which can be slow.

```python
from agent_workers.base import run_loop

# claim(), renewing(), and complete() belong to your application/runtime.
def tick(stop):
    job = claim()
    if job is None:
        return
    with renewing(job):
        result = worker.run(
            job["request"], key=job["id"], stop=stop.is_set, ensure_login=True,
        )
        accepted = complete(job, result)
    if accepted and result["state"] in {"answered", "resumed", "incomplete"}:
        worker.collect(result["entry"])

run_loop(tick, poll_seconds=15)
```

The example is an integration sketch: implement the three application functions for your queue and receiver. Delivery is at least once, so acceptance must be idempotent by task ID. Persist long-term history in your own storage. If delivery fails, leave the entry in the outbox so the same key can recover it.

## Usage and cost

Both adapters read subscription usage without asking the model:

| Provider | Primary mechanism | Fallback |
| --- | --- | --- |
| Claude | `get_usage` control request over stdio. | Human-readable `/usage`. |
| Codex | `app-server`: initialize, account/read, account/rateLimits/read. | Session rollout `token_count` snapshot. |

Measurements preserve their original timestamps. Fallback snapshots may be marked inexact; stale inexact data is handled by `stale_after` and `on_unknown`. Only windows relevant to the selected turn model are used. A reset during a turn does not become negative usage. After-measurements can be repeated to allow delayed usage accounting to settle.

Window values may be rounded to whole percentages; a short turn can report zero change. Provider credit availability can permit a turn even when a subscription window is exhausted, depending on `spend_credits`.

Token counts separate fresh input, cache reads, cache writes, and output. Reasoning is already included in output and is not charged twice. Codex cached input is subtracted from its total input, while Claude already reports the fresh-input remainder. Codex turn usage is read from rollout data, with streamed output as a fallback.

The cost is a comparison against API pricing, not a subscription charge. Claude's own reported value uses `source="cli"`; Codex uses the adapter's versioned model-price table. An unknown model price returns `None`. Review tables when prices or supported models change.

## Platforms and Docker

Production use targets Linux, commonly inside Docker. On Windows, development works, but surviving CLI descendants cannot always be cleaned up after their parent exits, and owner-only file permissions are not enforced as on Linux. Linux-only process/permission tests are skipped on Windows.

Sign in on the host first, then mount the account directory into the container and point `AGENT_HOME` at that mount. Login is interactive and a headless container does not supply the browser. Mount the runs directory persistently as well if it is outside the account mount.

A container needs Python 3.14+, Git, the package, and Node.js/provider CLI. The following is a building-block example; replace CLI version placeholders with versions verified in your deployment and install your application and this package:

```dockerfile
FROM node:24-bookworm-slim AS node
FROM python:3.14-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install -g @openai/codex@<version> @anthropic-ai/claude-code@<version> \
    && npm cache clean --force
ENV AGENT_PROVIDER=claude AGENT_HOME=/data/account DISABLE_AUTOUPDATER=1
```

Install Git in the image if fetching the package from its Git source. No GitHub token is needed for this public repository. Install only the provider CLIs your application uses.

Run with `docker run --init` or Compose `init: true`. The worker terminates the CLI process group after each turn; an init process reaps orphaned children. Ensure the mounted account and run directories belong to the container's user. Pin CLI versions because parsers depend on their output formats.

Claude also supports a headless token generated by `claude setup-token`; pass it as `CLAUDE_CODE_OAUTH_TOKEN`, which is allowed in the subprocess environment. Codex subscription login uses the mounted account directory.

The subprocess receives an explicit environment allowlist, including proxy and certificate settings such as `HTTPS_PROXY` and `NODE_EXTRA_CA_CERTS`. Project secrets outside that allowlist are not forwarded automatically.

## Architecture and testing

`base/` owns subprocess supervision, stdio, append-only run logs, locks, usage policy, cost arithmetic, and polling. `providers/` owns CLI flags, JSONL parsing, usage probes, and provider prices. `base/contract.py` defines the shared `Adapter`, `Command`, `Reply`, `Limits`, `Usage`, and `Cost` types. `tests/test_boundary.py` checks the dependency boundary.

Run:

```bash
uv sync
uv run python examples/outbox_demo.py
uv run ruff check .
uv run pytest
```

Tests use saved real CLI output in `tests/samples/` and subprocess fakes; they do not need installed provider CLIs or spend a subscription. `agent_workers.testing` provides `FakeAdapter`, `QUIET` (no settlement pauses), and `limits()` for downstream applications.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| No provider configured | Set `AGENT_PROVIDER` in `.env` or pass `--provider`. |
| CLI cannot be found/launched | Install the selected provider CLI; check `PATH` or `AGENT_<PROVIDER>_BINARY` and executable permissions. |
| Login check fails | Run `login` with the same provider and account directory used by the worker. |
| Different directories see different accounts | Check path resolution: `.env` paths are relative to the file; flag/environment paths are relative to the current directory. |
| Turn is refused by limits | Inspect `before`, the window reset, threshold, and credit policy. |
| Same key returns an older answer | Task keys identify work. Use a new key for a new task; collect only after accepting the previous result. |
| Entry is busy | A live local process holds its lock. Wait rather than deleting it. |
| Reply is incomplete | Inspect `reason` and `reply.diagnostic`; let the coordinator decide whether another paid attempt is appropriate. |
