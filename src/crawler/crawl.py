"""
ScreenWeave Playwright Crawler
==============================
Crawls a target URL and captures every visual state encountered, including
interactive UI changes (tabs, accordions, etc.), for downstream visual QA agents.

Artifacts written to OUT_DIR (/opt/output):
  .session_id               UUID of this run (read by the deploy bash script)
  screenshots/
    state_0001.png           One file per captured state
    state_0002.png
    ...
  states.json                Structured metadata for every state
  transitions.json           Edge list: how the crawler moved between states
  trace.zip                  Playwright trace (open with: playwright show-trace trace.zip)

The S3 folder structure produced by the calling bash script is:
  {S3_PREFIX}/{SESSION_ID}/
    states.json
    transitions.json
    trace.zip
    screenshots/
      state_0001.png
      ...

This matches the key convention expected by the ScreenWeave retrieval API
(infra/api-stack.yaml / src/lambda/getSession/index.js).
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from playwright.async_api import async_playwright

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL    = sys.argv[1]
OUT_DIR     = "/opt/output"
SCREENS_DIR = f"{OUT_DIR}/screenshots"
TRACE_PATH  = f"{OUT_DIR}/trace.zip"
SESSION_ID  = str(uuid.uuid4())

MAX_DEPTH = 2
MAX_LINKS = 12

os.makedirs(SCREENS_DIR, exist_ok=True)

# ── Runtime state (module-level mutables) ─────────────────────────────────────
seen        = set()      # visited URLs
states      = []         # accumulated state dicts
transitions = []         # accumulated transition dicts
state_ctr   = [0]        # monotonic state counter
prev_state  = [None]     # pointer to the last recorded state


# ── Helpers ───────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slug(url: str) -> str:
    p = urlparse(url)
    path = p.path.strip("/").replace("/", "_") or "home"
    return re.sub(r"[^a-z0-9_-]", "", path.lower())[:40] or "home"


def safe_label(text: str) -> str:
    """Normalise arbitrary element text to a safe ASCII label."""
    t = text.encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-z0-9 _-]", "", t.lower())
    t = re.sub(r"\s+", "-", t.strip())
    return t[:25] or "click"


def internal(url: str) -> bool:
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return False
    return p.netloc == urlparse(BASE_URL).netloc


# ── Page interactions ─────────────────────────────────────────────────────────

async def scroll_page(page) -> None:
    """Scroll from top to bottom so lazy-loaded content renders."""
    total = await page.evaluate("document.body.scrollHeight")
    vh    = await page.evaluate("window.innerHeight")
    step  = max(vh // 3, 200)
    pos   = 0
    while pos < total:
        await page.evaluate(f"window.scrollTo({{top:{pos},behavior:'smooth'}})")
        await asyncio.sleep(0.15)
        pos += step
    await page.evaluate("window.scrollTo({top:0,behavior:'smooth'})")
    await asyncio.sleep(0.3)


async def wait_for_idle(page, timeout: int = 5000) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        await asyncio.sleep(1.5)


# ── State capture ─────────────────────────────────────────────────────────────

async def capture_state(page, trigger_action: str = None, trigger_label: str = None) -> dict:
    """
    Record one visual state:
      1. Takes a full-page screenshot  → screenshots/state_NNNN.png
      2. Collects lightweight DOM metadata (headings, links, visible text)
      3. Appends a state dict to `states`
      4. Records a transition edge from the previous state (if any)

    Returns the new state dict.
    """
    state_ctr[0] += 1
    state_id  = f"state_{state_ctr[0]:04d}"
    ts        = now_iso()
    shot_path = f"{SCREENS_DIR}/{state_id}.png"

    await page.screenshot(path=shot_path, full_page=True)

    title = await page.title()
    url   = page.url

    try:
        scroll_y = await page.evaluate("window.scrollY")
        doc_h    = await page.evaluate("document.documentElement.scrollHeight")

        # Headings: give QA agents a content summary without reading the full DOM
        headings = await page.eval_on_selector_all(
            "h1,h2,h3",
            "els => els.slice(0,10).map(e => ({"
            "  tag: e.tagName.toLowerCase(),"
            "  text: e.innerText.trim().slice(0,120)"
            "}))"
        )

        # Unique internal + external links visible on the page
        links = await page.eval_on_selector_all(
            "a[href]",
            "els => [...new Set(els.map(e => e.href))].slice(0,25)"
        )

        # Short plaintext preview – useful for semantic diff between states
        preview = await page.evaluate(
            "document.body ? document.body.innerText.slice(0,600) : ''"
        )

        # Capture all visible interactive element labels for QA context
        interactive = await page.eval_on_selector_all(
            "[role=button],[role=tab],button,summary",
            "els => els.filter(e => e.offsetParent !== null)"
            "       .slice(0,20)"
            "       .map(e => e.innerText.trim().slice(0,60))"
            "       .filter(t => t.length > 0)"
        )
    except Exception:
        scroll_y, doc_h, headings, links, preview, interactive = 0, 0, [], [], "", []

    state = {
        "state_id":              state_id,
        "url":                   url,
        "title":                 title,
        "timestamp":             ts,
        "screenshot":            f"screenshots/{state_id}.png",
        "scroll_y":              scroll_y,
        "document_height":       doc_h,
        "trigger_action":        trigger_action,
        "trigger_label":         trigger_label,
        "headings":              headings,
        "links_found":           links,
        "interactive_elements":  interactive,
        "visible_text_preview":  preview.strip(),
    }
    states.append(state)

    print(f"STATE {state_id} | {trigger_action} | {url} | {title!r}", flush=True)

    # Record directed edge from previous state → this state
    if prev_state[0] is not None:
        transitions.append({
            "from_state_id": prev_state[0]["state_id"],
            "to_state_id":   state_id,
            "action":        trigger_action or "navigation",
            "trigger_label": trigger_label or "",
            "from_url":      prev_state[0]["url"],
            "to_url":        url,
            "timestamp":     ts,
        })

    prev_state[0] = state
    return state


# ── Interactive element clicking ───────────────────────────────────────────────

async def click_interactive(page) -> None:
    """
    Click every visible interactive element on the current page (tabs, buttons,
    accordions, toggles) and capture a state after each click.
    Skips elements that would navigate away from the current page.
    """
    selectors = [
        "[role=tab]",
        "[role=button]:not([disabled])",
        "button:not([disabled]):not([type=submit])",
        "[data-tab],[data-toggle],[data-target]",
        "details > summary",
        "[aria-selected=false]",
        "[aria-expanded=false]",
        ".accordion-header,.accordion-toggle",
    ]
    clicked_labels = set()
    click_count    = 0

    for sel in selectors:
        try:
            elements = await page.query_selector_all(sel)
            for el in elements[:8]:
                try:
                    if not await el.is_visible():
                        continue
                    if not await el.bounding_box():
                        continue

                    raw_text = (await el.inner_text()).strip()
                    label    = safe_label(raw_text)
                    if not label or label in clicked_labels:
                        continue

                    # Skip elements whose href would navigate to a different page
                    href = await el.get_attribute("href") or ""
                    if href and not href.startswith("#") and not href.startswith("javascript"):
                        if href.startswith("http"):
                            target = href
                        else:
                            target = BASE_URL.rstrip("/") + "/" + href.lstrip("/")
                        if urlparse(target).netloc != urlparse(BASE_URL).netloc:
                            continue
                        if urlparse(target).path != urlparse(page.url).path:
                            continue

                    clicked_labels.add(label)
                    print(f"  CLICK [{sel}] -> {label!r}", flush=True)
                    await el.scroll_into_view_if_needed()
                    await asyncio.sleep(0.2)
                    await el.click(timeout=3000)
                    await wait_for_idle(page, timeout=6000)
                    await asyncio.sleep(0.5)
                    await capture_state(page, trigger_action="click", trigger_label=label)
                    click_count += 1

                except Exception as e:
                    print(f"  CLICK ERR: {e}", flush=True)
        except Exception:
            pass

    if click_count:
        print(f"  Captured {click_count} interactive states on {page.url}", flush=True)


# ── Page crawl ────────────────────────────────────────────────────────────────

async def crawl(page, url: str, depth: int) -> None:
    if url in seen or depth > MAX_DEPTH:
        return
    seen.add(url)

    if urlparse(url).netloc != urlparse(BASE_URL).netloc:
        print(f"SKIP external {url}")
        return

    print(f"\nPAGE [{depth}] {url}", flush=True)
    try:
        await page.goto(url, wait_until="networkidle", timeout=25000)
        if urlparse(page.url).netloc != urlparse(BASE_URL).netloc:
            print(f"SKIP redirect {page.url}")
            return
    except Exception as e:
        print(f"ERR navigating to {url}: {e}")
        return

    await asyncio.sleep(1)

    # State 1: page as it loads (above the fold)
    await capture_state(page, trigger_action="navigation", trigger_label=url)

    # Scroll to reveal lazy-loaded content
    await scroll_page(page)

    # State 2: page after scrolling to bottom (full content visible)
    await capture_state(page, trigger_action="scroll", trigger_label="scroll_to_bottom")

    # States N+: one per interactive element clicked
    await click_interactive(page)

    # Recurse into child pages
    if depth < MAX_DEPTH:
        hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        kids  = list(
            dict.fromkeys([h for h in hrefs if internal(h) and h not in seen])
        )[:MAX_LINKS]
        for child_url in kids:
            await crawl(page, child_url, depth + 1)


# ── Title/outro card generation ────────────────────────────────────────────────

def card(path: str, l1: str, l2: str, l3: str = "") -> None:
    subprocess.run([
        "convert", "-size", "1280x800", "xc:#0d1117",
        "-fill", "#e6edf3", "-font", "DejaVu-Sans-Bold", "-pointsize", "56",
        "-gravity", "Center", "-annotate", "+0-80", l1,
        "-fill", "#58a6ff", "-font", "DejaVu-Sans",  "-pointsize", "32",
        "-gravity", "Center", "-annotate", "+0+20",  l2,
        "-fill", "#8b949e", "-pointsize", "22",
        "-gravity", "Center", "-annotate", "+0+100", l3,
        path,
    ], capture_output=True)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    print(f"SESSION_ID : {SESSION_ID}", flush=True)
    print(f"BASE_URL   : {BASE_URL}",   flush=True)
    print(f"OUT_DIR    : {OUT_DIR}",    flush=True)

    # Write session ID so the calling bash script can build the S3 path
    with open(f"{OUT_DIR}/.session_id", "w") as fh:
        fh.write(SESSION_ID)

    # Intro title card (not a crawled state, used only in the video)
    card(
        f"{OUT_DIR}/0000_title.png",
        "Tarun Raja",
        "Senior Lead SWE - AI Systems Architect",
        urlparse(BASE_URL).netloc,
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 Chrome/122",
        )

        # ── Playwright trace ──────────────────────────────────────────────────
        # Captures DOM snapshots and screenshots at every Playwright action.
        # Open the resulting trace.zip with:  playwright show-trace trace.zip
        await ctx.tracing.start(screenshots=True, snapshots=True, sources=False)

        page = await ctx.new_page()
        await crawl(page, BASE_URL, 0)

        # Export trace before the context closes
        await ctx.tracing.stop(path=TRACE_PATH)
        await browser.close()

    # Outro card (video only)
    card(
        f"{OUT_DIR}/9999_outro.png",
        urlparse(BASE_URL).netloc,
        "Courses - Projects - AI Architecture",
        "github.com/rajatarun",
    )

    # ── Write states manifest ─────────────────────────────────────────────────
    with open(f"{OUT_DIR}/states.json", "w") as fh:
        json.dump(
            {
                "session_id":   SESSION_ID,
                "base_url":     BASE_URL,
                "total_states": len(states),
                "captured_at":  now_iso(),
                "states":       states,
            },
            fh,
            indent=2,
        )
    print(f"Wrote states.json  ({len(states)} states)", flush=True)

    # ── Write transitions manifest ────────────────────────────────────────────
    with open(f"{OUT_DIR}/transitions.json", "w") as fh:
        json.dump(
            {
                "session_id":        SESSION_ID,
                "total_transitions": len(transitions),
                "transitions":       transitions,
            },
            fh,
            indent=2,
        )
    print(f"Wrote transitions.json ({len(transitions)} transitions)", flush=True)
    print(f"Wrote trace.zip", flush=True)
    print(f"\nDONE | {len(states)} states | {len(transitions)} transitions | SESSION_ID={SESSION_ID}", flush=True)


asyncio.run(main())
