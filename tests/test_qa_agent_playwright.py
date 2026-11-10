"""QA agent end to end on a real browser against a local page.

A specific instruction runs directly as the plan through PlaywrightExecutor
(selector first, intent locator with the fake model second). Served from
127.0.0.1 over HTTP so the policy's domain allowlist applies as in production.
Skipped when Playwright or Chromium is not installed.
"""
from __future__ import annotations

import asyncio
import functools
import http.server
import pathlib
import sys
import threading

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "crawler"))
sys.path.insert(0, str(ROOT / "src" / "lambda" / "visualQAWorker"))

import intent_locator as il  # noqa: E402
import qa_agent as qa  # noqa: E402
import signoff_chain as sc  # noqa: E402

for _name in ("playwright.async_api", "playwright"):
    _mod = sys.modules.get(_name)
    if _mod is not None and getattr(_mod, "__file__", None) is None:
        del sys.modules[_name]
async_api = pytest.importorskip("playwright.async_api")


@pytest.fixture(scope="module")
def base_url():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(ROOT / "tests" / "fixtures" / "intent"))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/"
    srv.shutdown()


def _run(base_url, instruction, tmp_path):
    url = base_url + "qa.html"
    store = qa.InMemoryQARunStore({"run-pw": {
        "run_id": "run-pw", "url": url, "instruction": instruction,
        "environment": {"url": url}, "messages": [],
        "attempts": [{"n": 1, "adjustments": {}}], "limits": {"max_cases": 3, "max_steps": 10}}})

    async def go():
        async with async_api.async_playwright() as p:
            try:
                browser = await p.chromium.launch()
            except Exception as exc:
                pytest.skip(f"Chromium not available: {exc}")
            try:
                page = await browser.new_page()
                ex = qa.PlaywrightExecutor(page, finder=il.IntentFinder(il.FakeVisionModel()),
                                           intent_enabled=True, out_dir=str(tmp_path))
                return await qa.QAAgent(store, signoff_module=sc).run_attempt("run-pw", 1, ex)
            finally:
                await browser.close()

    return asyncio.run(go()), store


def test_specific_instruction_passes_end_to_end(base_url, tmp_path):
    out, store = _run(base_url, 'fill "jane@example.com" into email, then click Checkout, '
                                'then verify "Order placed for jane@example.com"', tmp_path)
    case = out["results"]["cases"][0]
    assert case["status"] == "passed", case
    assert [s["action"] for s in case["steps"]] == ["navigate", "fill", "click", "assert_text"]
    assert case["steps"][2]["detail"] == "intent", "no selector given: the intent locator found it"
    assert out["signoff"]["findings"] == []
    assert store.events.index(("plan_saved", "run-pw", 1)) < store.events.index(("status", "run-pw", 1, "RUNNING"))


def test_failed_expectation_becomes_a_finding_with_a_screenshot(base_url, tmp_path):
    out, _ = _run(base_url, 'click Checkout then verify "Payment declined"', tmp_path)
    case = out["results"]["cases"][0]
    assert case["status"] == "failed"
    f = out["signoff"]["findings"][0]
    assert f["severity"] == "critical" and f["status"] == "escalated"
    assert (tmp_path / "TC-1_step03.png").exists()
