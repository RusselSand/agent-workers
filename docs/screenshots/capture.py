"""Render the real offline-example transcript; requires optional playwright."""

import html
import os
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(__file__).resolve().parent


def main():
    completed = subprocess.run(
        [sys.executable, str(ROOT / "examples/outbox_demo.py")],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    transcript = completed.stdout
    assert "state=answered" in transcript and "state=resumed" in transcript
    assert "Adapter invocations 1" in transcript
    OUTPUT.joinpath("outbox-demo.txt").write_text(transcript, encoding="utf-8")
    document = """<!doctype html><html lang="en"><meta charset="utf-8">
    <title>agent-workers offline example</title><style>
    * { box-sizing: border-box; }
    body { margin: 0; padding: 44px; background: #edf2f7; color: #101828;
           font-family: Arial, sans-serif; }
    header { margin-bottom: 26px; }
    h1 { font-size: 32px; letter-spacing: -1px; margin: 0 0 10px; }
    header p { font-size: 16px; color: #526174; margin: 0; }
    article { border-radius: 18px; overflow: hidden; background: #111827;
              box-shadow: 0 18px 44px #26344925; }
    .bar { background: #1f2937; padding: 17px 24px; color: #c5cedb; font-size: 14px; }
    .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
           background: #8b9bb0; margin-right: 6px; }
    .command { color: #a7f3d0; margin-bottom: 22px; }
    section { padding: 26px 30px; }
    pre { color: #e5eaf2; font: 17px/1.65 "DejaVu Sans Mono", monospace; margin: 0;
          white-space: pre-wrap; }
    footer { color: #526174; font-size: 14px; margin-top: 22px; }
    </style><header><h1>One task. One invocation. A recoverable answer.</h1>
    <p>agent-workers · Python API and local outbox</p></header><article>
    <div class="bar"><span class="dot"></span><span class="dot"></span>
    <span class="dot"></span> Offline lifecycle example</div><section>
    <pre class="command">$ uv run python examples/outbox_demo.py</pre><pre>"""
    document += html.escape(transcript)
    document += """</pre></section></article><footer>
    Actual example output · FakeAdapter · simulated usage and price · no provider account required
    </footer></html>"""
    with sync_playwright() as playwright:
        options = {}
        if executable := os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"):
            options = {"executable_path": executable,
                       "args": ["--no-sandbox", "--no-zygote", "--single-process", "--disable-gpu"]}
        browser = playwright.chromium.launch(**options)
        try:
            page = browser.new_page(viewport={"width": 1160, "height": 800}, device_scale_factor=1)
            page.set_content(document)
            page.evaluate("document.fonts.ready")
            page.screenshot(path=str(OUTPUT / "outbox-demo.png"), full_page=True)
        finally:
            browser.close()
    print("Captured docs/screenshots/outbox-demo.png")


if __name__ == "__main__":
    main()
