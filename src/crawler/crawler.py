import asyncio, os, re, subprocess, sys, time, json, uuid
from datetime import datetime, timezone
from urllib.parse import urlparse
from playwright.async_api import async_playwright

BASE_URL     = sys.argv[1]
SESSION_ID   = sys.argv[2] if len(sys.argv) > 2 else (
    f"sess_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
)
SCREENS      = "/opt/output/screens"
MAX_DEPTH    = 2
MAX_LINKS    = 12
os.makedirs(SCREENS, exist_ok=True)

seen         = set()
ctr          = [0]
state_ctr    = [0]
trans_ctr    = [0]
states       = []
transitions  = []


# ── Utilities ────────────────────────────────────────────────────────────────

def slug(url):
    p = urlparse(url)
    path = p.path.strip("/").replace("/", "_") or "home"
    return re.sub(r"[^a-z0-9_-]", "", path.lower())[:40] or "home"


def safe_label(text):
    t = text.encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-z0-9 _-]", "", t.lower())
    t = re.sub(r"\s+", "-", t.strip())
    return t[:25] or "click"


def internal(url):
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return False
    return p.netloc == urlparse(BASE_URL).netloc


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Metrics collection ────────────────────────────────────────────────────────

async def collect_page_metrics(page, url, title, state_id, ts, load_time_ms, console_error_count):
    """Collect DOM and performance metrics from the live page."""
    try:
        m = await page.evaluate("""() => {
            const all          = document.querySelectorAll('*').length;
            const interactive  = document.querySelectorAll(
                'a,button,input,select,textarea,[role=button],[role=tab],[role=link]'
            ).length;
            const images       = document.querySelectorAll('img').length;
            const links        = document.querySelectorAll('a[href]').length;
            const scrollHeight = document.body.scrollHeight;
            const h1           = document.querySelector('h1');
            const heading      = h1 ? h1.innerText.trim().slice(0, 80) : '';
            const metaTags     = document.querySelectorAll('meta').length;
            const forms        = document.querySelectorAll('form').length;
            return {
                dom_elements: all,
                interactive_elements: interactive,
                images,
                links,
                scroll_height: scrollHeight,
                viewport_width: 1280,
                viewport_height: 800,
                heading,
                meta_tags: metaTags,
                forms
            };
        }""")
    except Exception:
        m = {}

    m["load_time_ms"]    = load_time_ms
    m["console_errors"]  = console_error_count
    return {
        "state_id":  state_id,
        "url":       url,
        "title":     title,
        "screenshot": f"screenshots/{state_id}.png",
        "metrics":   m,
        "timestamp": ts,
    }


# ── Page interaction helpers ──────────────────────────────────────────────────

async def scroll(page):
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


async def shot(page, label, state_id):
    path = f"{SCREENS}/{state_id}.png"
    await page.screenshot(path=path, full_page=True)
    ctr[0] += 1
    print(f"SHOT {path}", flush=True)
    return state_id


async def wait_for_idle(page, timeout=5000):
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        await asyncio.sleep(1.5)


async def click_interactive(page, lbl, console_errors, parent_state_id):
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
    clicked_texts = set()
    click_count   = 0

    for sel in selectors:
        try:
            els = await page.query_selector_all(sel)
            for el in els[:8]:
                try:
                    if not await el.is_visible():
                        continue
                    box = await el.bounding_box()
                    if not box:
                        continue
                    raw_text = (await el.inner_text()).strip()
                    label    = safe_label(raw_text)
                    if not label or label in clicked_texts:
                        continue
                    href = await el.get_attribute("href") or ""
                    if href and not href.startswith("#") and not href.startswith("javascript"):
                        target = (
                            href if href.startswith("http")
                            else BASE_URL.rstrip("/") + "/" + href.lstrip("/")
                        )
                        if urlparse(target).netloc != urlparse(BASE_URL).netloc:
                            continue
                        if urlparse(target).path != urlparse(page.url).path:
                            continue
                    clicked_texts.add(label)
                    print(f"  CLICK [{sel}] -> {label!r}", flush=True)

                    await el.scroll_into_view_if_needed()
                    await asyncio.sleep(0.2)

                    t_start   = time.monotonic()
                    from_url  = page.url
                    err_before = len(console_errors)

                    await el.click(timeout=3000)
                    await wait_for_idle(page, timeout=6000)
                    await asyncio.sleep(0.5)
                    t_ms = round((time.monotonic() - t_start) * 1000)

                    # Screenshot the new state
                    state_ctr[0] += 1
                    new_sid = f"{state_ctr[0]:04d}_{lbl}_tab{click_count:02d}_{label}"
                    await shot(page, new_sid, new_sid)

                    # Collect metrics for the new state
                    title = await page.title()
                    ts    = now_iso()
                    page_state = await collect_page_metrics(
                        page, page.url, title, new_sid, ts,
                        load_time_ms=t_ms,
                        console_error_count=len(console_errors) - err_before
                    )
                    states.append(page_state)

                    # Record transition
                    trans_ctr[0] += 1
                    transitions.append({
                        "transition_id":    f"t_{trans_ctr[0]:04d}",
                        "from_state_id":    parent_state_id,
                        "to_state_id":      new_sid,
                        "from_url":         from_url,
                        "to_url":           page.url,
                        "trigger":          "click",
                        "element_text":     raw_text[:100],
                        "element_type":     (sel.split("[")[0] or "element").strip(":"),
                        "transition_time_ms": t_ms,
                        "timestamp":        ts,
                    })
                    click_count += 1
                except Exception as e:
                    print(f"  CLICK ERR: {e}", flush=True)
        except Exception:
            pass

    if click_count:
        print(f"  Clicked {click_count} interactive elements", flush=True)


# ── Crawler ───────────────────────────────────────────────────────────────────

async def crawl(page, url, depth, console_errors, parent_state_id=None):
    if url in seen or depth > MAX_DEPTH:
        return
    seen.add(url)
    if urlparse(url).netloc != urlparse(BASE_URL).netloc:
        print(f"SKIP external {url}")
        return
    print(f"PAGE [{depth}] {url}", flush=True)

    t_start = time.monotonic()
    try:
        await page.goto(url, wait_until="networkidle", timeout=25000)
        if urlparse(page.url).netloc != urlparse(BASE_URL).netloc:
            print(f"SKIP redirect {page.url}")
            return
    except Exception as e:
        print(f"ERR {e}")
        return
    await asyncio.sleep(1)

    load_ms = round((time.monotonic() - t_start) * 1000)
    lbl     = slug(url)
    ts      = now_iso()

    # Top-of-page screenshot and metrics
    state_ctr[0] += 1
    sid_top = f"{state_ctr[0]:04d}_{lbl}_top"
    await shot(page, lbl + "_top", sid_top)
    title       = await page.title()
    page_state  = await collect_page_metrics(
        page, url, title, sid_top, ts,
        load_time_ms=load_ms,
        console_error_count=len(console_errors)
    )
    states.append(page_state)

    # Record navigation transition from parent
    if parent_state_id:
        trans_ctr[0] += 1
        transitions.append({
            "transition_id":    f"t_{trans_ctr[0]:04d}",
            "from_state_id":    parent_state_id,
            "to_state_id":      sid_top,
            "from_url":         "",
            "to_url":           url,
            "trigger":          "navigation",
            "element_text":     "",
            "element_type":     "link",
            "transition_time_ms": load_ms,
            "timestamp":        ts,
        })

    # Scroll and bottom screenshot
    await scroll(page)
    state_ctr[0] += 1
    sid_bot = f"{state_ctr[0]:04d}_{lbl}_bot"
    await shot(page, lbl + "_bot", sid_bot)

    # Click interactive elements, record transitions
    await click_interactive(page, lbl, console_errors, parent_state_id=sid_top)

    # Recurse
    if depth < MAX_DEPTH:
        hrefs = await page.eval_on_selector_all("a[href]", "els=>els.map(e=>e.href)")
        kids  = list(dict.fromkeys(
            [h for h in hrefs if internal(h) and h not in seen]
        ))[:MAX_LINKS]
        for k in kids:
            await crawl(page, k, depth + 1, console_errors, parent_state_id=sid_top)


# ── Title cards ───────────────────────────────────────────────────────────────

def card(path, l1, l2, l3=""):
    subprocess.run([
        "convert", "-size", "1280x800", "xc:#0d1117",
        "-fill", "#e6edf3", "-font", "DejaVu-Sans-Bold", "-pointsize", "56",
        "-gravity", "Center", "-annotate", "+0-80", l1,
        "-fill", "#58a6ff", "-font", "DejaVu-Sans", "-pointsize", "32",
        "-gravity", "Center", "-annotate", "+0+20", l2,
        "-fill", "#8b949e", "-pointsize", "22",
        "-gravity", "Center", "-annotate", "+0+100", l3,
        path,
    ], capture_output=True)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    crawl_start    = time.monotonic()
    console_errors = []

    card(
        "/opt/output/0000_title.png",
        "Tarun Raja",
        "Senior Lead SWE - AI Systems Architect",
        urlparse(BASE_URL).netloc,
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx     = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 Chrome/122",
        )
        page = await ctx.new_page()
        page.on("console", lambda m: console_errors.append(m) if m.type == "error" else None)
        await crawl(page, BASE_URL, 0, console_errors)
        await browser.close()

    card(
        "/opt/output/9999_outro.png",
        urlparse(BASE_URL).netloc,
        "Courses - Projects - AI Architecture",
        "github.com/rajatarun",
    )

    crawl_duration_ms = round((time.monotonic() - crawl_start) * 1000)

    # Write states.json
    states_doc = {
        "session_id":        SESSION_ID,
        "base_url":          BASE_URL,
        "generated_at":      now_iso(),
        "crawl_duration_ms": crawl_duration_ms,
        "states":            states,
    }
    with open("/opt/output/states.json", "w") as f:
        json.dump(states_doc, f, indent=2)

    # Write transitions.json
    transitions_doc = {
        "session_id":   SESSION_ID,
        "generated_at": now_iso(),
        "transitions":  transitions,
    }
    with open("/opt/output/transitions.json", "w") as f:
        json.dump(transitions_doc, f, indent=2)

    print(
        f"DONE {ctr[0]} screenshots | "
        f"{len(states)} states | "
        f"{len(transitions)} transitions | "
        f"{crawl_duration_ms}ms",
        flush=True,
    )


asyncio.run(main())
