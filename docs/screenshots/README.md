# Offline example screenshot

`outbox-demo.png` is a browser-rendered terminal-style view of the actual stdout from [`examples/outbox_demo.py`](../../examples/outbox_demo.py). The same output is saved in [`outbox-demo.txt`](outbox-demo.txt).

The example uses the real worker and local outbox with `FakeAdapter`. It launches a Python subprocess rather than Claude Code or Codex. Usage and API price values are simulated. It makes no model calls, uses no provider credentials, and asserts that retrying a completed key does not invoke the adapter again.

## Reproduce

From the repository root, install the project and the optional screenshot browser:

```bash
uv sync
uv run --with playwright python -m playwright install chromium
uv run --with playwright python docs/screenshots/capture.py
```

Playwright is a documentation-only tool, not a package runtime dependency. The script runs the example, captures its real stdout, escapes it as text, and renders it at 1160 px wide with all-English labels. It does not rewrite the output. To use an existing Chromium installation, set `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` to its executable path.

The example creates account and run folders in a temporary directory and removes them when finished. The screenshot and text transcript are the only saved outputs.
