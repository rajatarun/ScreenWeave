"""Intent locator against a real browser and local HTML fixtures.

v1.html has the checkout button at #checkout-btn in the header. v2.html is a
redesign: no id, new classes, a styled div at the bottom of the page, and a
different visible label. The v1 selector misses on v2; the intent finder
(fake model, no network) still finds the control. shift.html re-renders the
control right after load, so the element resolved first detaches.

Skipped when Playwright or its Chromium build is not installed (CI installs
neither). Locally:  pip install playwright && python -m playwright install chromium
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "intent"
sys.path.insert(0, str(ROOT / "src" / "crawler"))

import intent_locator as il  # noqa: E402

# test_crawl_guard.py puts placeholder playwright modules in sys.modules so
# crawl.py imports without the real package. Drop them to reach the real one.
for _name in ("playwright.async_api", "playwright"):
    _mod = sys.modules.get(_name)
    if _mod is not None and getattr(_mod, "__file__", None) is None:
        del sys.modules[_name]

async_api = pytest.importorskip("playwright.async_api")

SELECTOR = "#checkout-btn"
INTENT = "checkout"


def _run(coro):
    return asyncio.run(coro)


async def _with_page(fn):
    async with async_api.async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as exc:  # browser build not installed
            pytest.skip(f"Chromium not available: {exc}")
        try:
            page = await browser.new_page(viewport={"width": 1024, "height": 700})
            return await fn(page)
        finally:
            await browser.close()


def _url(name):
    return (FIXTURES / name).as_uri()


def test_selector_wins_when_it_still_matches():
    model = il.FakeVisionModel()

    async def go(page):
        await page.goto(_url("v1.html"))
        res = await il.resolve(page, SELECTOR, INTENT, il.IntentFinder(model), enabled=True)
        await res.locator.click()
        return res, await page.evaluate("document.body.dataset.result")

    res, result = _run(_with_page(go))
    assert res.strategy == "selector"
    assert result == "checked-out"
    assert model.calls == [], "no model call when the selector works"


def test_intent_finder_recovers_when_the_redesign_breaks_the_selector():
    model = il.FakeVisionModel()

    async def go(page):
        await page.goto(_url("v2.html"))
        res = await il.resolve(page, SELECTOR, INTENT, il.IntentFinder(model), enabled=True)
        box = await res.locator.bounding_box()
        await res.locator.click()
        return res, box, await page.evaluate("document.body.dataset.result")

    res, box, result = _run(_with_page(go))
    assert res.strategy == "intent"
    assert res.attempts[0]["reason"] == "selector matched nothing"
    assert res.match.candidate.role == "button" and res.match.candidate.tag == "div"
    assert box["y"] > 700, "the control moved below the first viewport"
    assert result == "checked-out", "clicked checkout, not a distractor"
    assert model.calls[0]["has_screenshot"]


def test_disabled_flag_fails_safe_without_clicking():
    model = il.FakeVisionModel()

    async def go(page):
        await page.goto(_url("v2.html"))
        res = await il.resolve(page, SELECTOR, INTENT, il.IntentFinder(model), enabled=False)
        return res, await page.evaluate("document.body.dataset.result || null")

    res, result = _run(_with_page(go))
    assert res.strategy == "none" and not res.found
    assert "disabled" in res.reason
    assert result is None and model.calls == []


def test_no_confident_match_fails_safe():
    async def go(page):
        await page.goto(_url("v2.html"))
        res = await il.resolve(page, SELECTOR, "delete my account",
                               il.IntentFinder(il.FakeVisionModel()), enabled=True)
        return res, await page.evaluate("document.body.dataset.result || null")

    res, result = _run(_with_page(go))
    assert not res.found and result is None


def test_re_resolves_after_layout_shift():
    model = il.FakeVisionModel()

    async def go(page):
        await page.goto(_url("shift.html"))
        res = await il.resolve_stable(page, SELECTOR, INTENT, il.IntentFinder(model),
                                      enabled=True, settle_ms=700)
        await res.locator.click()
        return res, await page.evaluate("document.body.dataset.result")

    res, result = _run(_with_page(go))
    strategies = [a["strategy"] for a in res.attempts]
    assert strategies[0] == "selector" and res.attempts[0]["ok"], "first hit the old element"
    assert "layout-shift" in strategies
    assert res.strategy == "intent"
    assert result == "checked-out", "clicked the re-rendered control, not the stale one"
    assert il.resolution_log(res)["found"] is True
