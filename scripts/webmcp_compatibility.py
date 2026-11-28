#!/usr/bin/env python3
"""task.optional_webmcp.011: run the WebMCP page in every browser configuration available here.

  python scripts/webmcp_compatibility.py --output evidence/optional/webmcp-compatibility.json

For each configuration the page is loaded from the Python host (or, for the insecure case,
from a non-loopback http origin) and the record keeps: the browser version, whether WebMCP
was detected and why not, the tools the browser lists after registration, an agent call and
a refused agent call, and whether the ordinary page still works by a user click. Needs
requirements-optional-py313.lock (Playwright 1.63.0, which drives Chromium 158).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from playwright.sync_api import sync_playwright  # noqa: E402

from vater_optional.webmcp_host import WEB, Host, create_app, serve  # noqa: E402

FLAG = "--enable-experimental-web-platform-features"
CONFIGURATIONS = {
    "chromium-headless-shell, experimental flag": {"channel": None, "args": [FLAG], "secure": True},
    "chromium-headless-shell, default": {"channel": None, "args": [], "secure": True},
    "chromium (new headless), experimental flag": {"channel": "chromium", "args": [FLAG], "secure": True},
    "chromium (new headless), default": {"channel": "chromium", "args": [], "secure": True},
    "chromium-headless-shell, experimental flag, insecure origin": {"channel": None, "args": [FLAG],
                                                                   "secure": False},
}
AGENT_CALLS = """async () => {
  const context = document.modelContext;
  if (!context) return null;
  const tools = await context.getTools();
  const add = tools.find(tool => tool.name === "add_numbers");
  const parse = text => JSON.parse(JSON.parse(text).content[0].text);
  return {
    tools: tools.map(tool => tool.name).sort(),
    call: parse(await context.executeTool(add, {left: 40, right: 2})).status,
    refused: parse(await context.executeTool(add, {left: -1, right: 2})).status,
  };
}"""


def probe(playwright, name: str, configuration: dict, port: int) -> dict:
    launch = {"args": configuration["args"]}
    if configuration["channel"]:
        launch["channel"] = configuration["channel"]
    browser = playwright.chromium.launch(**launch)
    try:
        page = browser.new_page()
        if configuration["secure"]:
            page.goto(f"http://127.0.0.1:{port}/")
        else:  # a plain-http, non-loopback origin is not a secure context
            files = {"/": ("index.html", "text/html"), "/vater-webmcp.js": ("vater-webmcp.js", "text/javascript"),
                     "/style.css": ("style.css", "text/css")}

            def fulfil(route):
                path = route.request.url.split("http://webmcp.test", 1)[1]
                if path in files:
                    filename, kind = files[path]
                    route.fulfill(body=(WEB / "webmcp" / filename).read_bytes(), content_type=kind)
                else:
                    route.abort()
            page.route("http://webmcp.test/**", fulfil)
            page.goto("http://webmcp.test/")
        page.wait_for_selector("body[data-ready=true]")
        status = page.locator("#webmcp-status")
        agent = page.evaluate(AGENT_CALLS)
        page.fill("#left", "2")
        page.fill("#right", "3")
        page.click("#add-page")
        page.wait_for_selector("#activity li[data-source=user]")
        return {
            "configuration": name, "browser": browser.version, "flags": configuration["args"],
            "secure_context": page.evaluate("isSecureContext"),
            "webmcp_detected": status.get_attribute("data-supported") == "true",
            "reason": status.get_attribute("data-reason"),
            "registered_tools": agent["tools"] if agent else [],
            "agent_call": agent["call"] if agent else None,
            "agent_invalid_call": agent["refused"] if agent else None,
            "user_click_works": page.locator("#activity li[data-source=user]").first.get_attribute("data-status")
            == "completed",
        }
    finally:
        browser.close()


def probe_all(playwright=None) -> list[dict]:
    """Probe every configuration; reuse a running Playwright when the caller already has one."""
    with serve(create_app(Host("compatibility-operator-0123456789abcdef"))) as port:
        if playwright is not None:
            return [probe(playwright, name, configuration, port) for name, configuration in CONFIGURATIONS.items()]
        with sync_playwright() as running:
            return [probe(running, name, configuration, port) for name, configuration in CONFIGURATIONS.items()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = probe_all()
    record = {"playwright": "1.63.0", "results": results,
              "not_available_here": ["Firefox", "WebKit (Safari)", "Microsoft Edge", "Chrome stable with a GPU"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    for result in results:
        print(result["configuration"], result["reason"], result["registered_tools"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
