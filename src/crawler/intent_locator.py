"""
ScreenWeave Intent Locator
==========================
Finds a page element by what it is for ("the checkout button") instead of by
a CSS selector that breaks the next time the layout changes.

This is the first slice of the hybrid architecture (.claude/architecture.md):

  Deterministic layer   Playwright selectors, exactly as crawl.py uses today.
  Intent layer          This module. A vision-and-language model looks at a
                        screenshot plus a compact list of candidate elements
                        (role, accessible name, text, bounding box) and picks
                        the one that matches an intent, with a confidence and
                        a rationale, or says nothing matches.

Fallback order (resolve()):
  1. The selector, when it matches exactly one visible element.
  2. The intent finder, only when enabled and only at or above min_confidence.
  3. Fail safe: no element, no action. The caller decides what to do next.

Nothing here is wired into crawl.py. It is opt-in: set
SCREENWEAVE_INTENT_LOCATOR=on (or pass enabled=True) to allow step 2.

The model is behind a small protocol (VisionModelClient) so the finder is
provider-agnostic:
  BedrockVisionModel   Anthropic models on Bedrock via invoke_model, the same request
                       shape the Visual QA worker already sends.
  FakeVisionModel      Deterministic lexical matcher for tests and offline
                       runs. No network, no cost.

Playwright is not imported here: functions take a Playwright Page (async API)
as an argument, so the module imports cleanly anywhere.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, Sequence

# ── Configuration ─────────────────────────────────────────────────────────────

FLAG_ENV = "SCREENWEAVE_INTENT_LOCATOR"
DEFAULT_MIN_CONFIDENCE = float(os.environ.get("SCREENWEAVE_INTENT_MIN_CONFIDENCE", "0.6"))
DEFAULT_MAX_CANDIDATES = 40          # caps prompt size (and cost) per lookup
CANDIDATE_ATTR = "data-sw-candidate"  # set on candidates during observe()

DEFAULT_BEDROCK_MODEL = os.environ.get(
    "SCREENWEAVE_INTENT_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)
_BACKOFF = (2, 4, 8)  # same throttle schedule as the Visual QA worker


def intent_locator_enabled(env: dict | None = None) -> bool:
    """True only when SCREENWEAVE_INTENT_LOCATOR is explicitly switched on."""
    value = (env if env is not None else os.environ).get(FLAG_ENV, "")
    return value.strip().lower() in ("1", "true", "on", "yes")


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BBox:
    x: float
    y: float
    width: float
    height: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2, self.y + self.height / 2)

    def moved_from(self, other: "BBox", tolerance: float = 2.0) -> bool:
        return any(
            abs(a - b) > tolerance
            for a, b in ((self.x, other.x), (self.y, other.y),
                         (self.width, other.width), (self.height, other.height))
        )


@dataclass(frozen=True)
class Candidate:
    """One element the model may choose. Built from the DOM/accessibility tree."""
    id: str                     # stable within one observation ("c0", "c1", ...)
    tag: str
    role: str = ""
    name: str = ""              # accessible name: aria-label, label, alt, title, text
    text: str = ""
    bbox: BBox | None = None
    attributes: dict = field(default_factory=dict)  # id, type, href, placeholder...

    def prompt_dict(self) -> dict:
        d = {"id": self.id, "tag": self.tag, "role": self.role,
             "name": self.name, "text": self.text}
        if self.bbox:
            d["bbox"] = [round(self.bbox.x), round(self.bbox.y),
                         round(self.bbox.width), round(self.bbox.height)]
        if self.attributes:
            d["attributes"] = self.attributes
        return d


@dataclass
class Observation:
    url: str
    candidates: list[Candidate]
    screenshot: bytes | None = None
    viewport: tuple[int, int] | None = None
    title: str = ""


@dataclass(frozen=True)
class ModelChoice:
    """Raw answer from a VisionModelClient, before validation."""
    candidate_id: str | None
    confidence: float
    rationale: str


@dataclass(frozen=True)
class Match:
    candidate: Candidate
    confidence: float
    rationale: str


@dataclass(frozen=True)
class NoMatch:
    reason: str
    confidence: float = 0.0
    rationale: str = ""


@dataclass
class Resolution:
    """Outcome of resolve(). strategy is 'selector', 'intent' or 'none'."""
    strategy: str
    locator: Any = None          # Playwright Locator, or None (fail safe)
    match: Match | None = None
    reason: str = ""
    attempts: list[dict] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.locator is not None


# ── Model clients ─────────────────────────────────────────────────────────────

class VisionModelClient(Protocol):
    """Anything that can pick one candidate for an intent."""

    def choose(self, intent: str, observation: Observation,
               candidates: Sequence[Candidate]) -> ModelChoice: ...


_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset({"the", "a", "an", "to", "on", "of", "for", "and", "or",
                   "button", "link", "click", "press", "tap", "field", "input"})


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD.findall(text.lower()) if t not in _STOP}


def lexical_score(intent: str, candidate: Candidate) -> float:
    """Share of the intent's words found in the candidate's name/text/attrs.

    Used by FakeVisionModel and to pre-rank candidates so a page with hundreds
    of elements sends only the most plausible ones to a paid model.
    """
    want = _tokens(intent)
    if not want:
        return 0.0
    hay = " ".join([candidate.name, candidate.text, candidate.role,
                    " ".join(str(v) for v in candidate.attributes.values())])
    have = _tokens(hay)
    return len(want & have) / len(want)


class FakeVisionModel:
    """Deterministic stand-in for a vision-language model.

    Picks the candidate with the highest lexical_score (ties: first in list).
    Records every call so tests can assert on what the model was shown.
    """

    def __init__(self, fixed: ModelChoice | None = None):
        self.fixed = fixed
        self.calls: list[dict] = []

    def choose(self, intent, observation, candidates):
        self.calls.append({"intent": intent, "url": observation.url,
                           "candidate_ids": [c.id for c in candidates],
                           "has_screenshot": observation.screenshot is not None})
        if self.fixed is not None:
            return self.fixed
        best, best_score = None, 0.0
        for c in candidates:
            s = lexical_score(intent, c)
            if s > best_score:
                best, best_score = c, s
        if best is None:
            return ModelChoice(None, 0.0, "no candidate shares words with the intent")
        return ModelChoice(best.id, round(best_score, 3),
                           f"lexical overlap {best_score:.2f} with {best.name or best.text!r}")


_SYSTEM_PROMPT = (
    "You locate one element on a web page for a QA automation tool. You are given "
    "a screenshot and a JSON list of candidate elements with ids, roles, accessible "
    "names, text and bounding boxes [x, y, width, height] in CSS pixels. Choose the "
    "single candidate that best matches the user's intent. If none clearly matches, "
    "answer with candidate_id null. Never invent an id that is not in the list. "
    "Reply with JSON only: "
    '{"candidate_id": "<id or null>", "confidence": <0..1>, "rationale": "<one sentence>"}'
)


def parse_model_json(text: str) -> ModelChoice:
    """Parse the model's JSON answer, tolerating a ```json fence or extra prose."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return ModelChoice(None, 0.0, "model reply was not JSON")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return ModelChoice(None, 0.0, "model reply was not valid JSON")
    cid = data.get("candidate_id")
    try:
        conf = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(conf, 1.0))
    return ModelChoice(str(cid) if cid not in (None, "", "null") else None,
                       conf, str(data.get("rationale", ""))[:500])


class BedrockVisionModel:
    """Anthropic models on Amazon Bedrock, called the way visualQAWorker/handler.py does:
    invoke_model with an anthropic_version body, base64 image blocks, and
    exponential backoff on ThrottlingException / ServiceUnavailableException.

    The boto3 client is created lazily (or injected), so importing this module
    never needs AWS credentials.
    """

    def __init__(self, model_id: str = DEFAULT_BEDROCK_MODEL, client=None,
                 region: str | None = None, max_tokens: int = 300, sleep=time.sleep):
        self.model_id = model_id
        self._client = client
        self.region = region or os.environ.get("SCREENWEAVE_REGION",
                                               os.environ.get("AWS_REGION", "us-east-1"))
        self.max_tokens = max_tokens
        self._sleep = sleep

    @property
    def client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    def build_body(self, intent: str, observation: Observation,
                   candidates: Sequence[Candidate]) -> dict:
        content: list[dict] = []
        if observation.screenshot:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": base64.b64encode(observation.screenshot).decode("ascii")}})
        content.append({"type": "text", "text": (
            f"Page: {observation.title or ''} {observation.url}\n"
            f"Intent: {intent}\n"
            f"Candidates:\n{json.dumps([c.prompt_dict() for c in candidates])}"
        )})
        return {"anthropic_version": "bedrock-2023-05-31",
                "max_tokens": self.max_tokens,
                "system": _SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": content}]}

    def choose(self, intent, observation, candidates):
        from botocore.exceptions import ClientError
        body = json.dumps(self.build_body(intent, observation, candidates))
        last_exc: Exception | None = None
        for delay in [0] + list(_BACKOFF):
            if delay:
                self._sleep(delay)
            try:
                resp = self.client.invoke_model(
                    modelId=self.model_id, contentType="application/json",
                    accept="application/json", body=body)
                result = json.loads(resp["body"].read())
                return parse_model_json(result["content"][0]["text"])
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in ("ThrottlingException", "ServiceUnavailableException"):
                    last_exc = exc
                    continue
                raise
        raise RuntimeError(f"Bedrock still throttling after {len(_BACKOFF)} retries") from last_exc


# ── Finder ────────────────────────────────────────────────────────────────────

class IntentFinder:
    """Validates and thresholds a model's choice.

    - Drops invisible / zero-size candidates and caps the list sent to the model
      (pre-ranked by lexical_score, so the likely ones survive the cap).
    - Rejects ids the model invented.
    - Returns NoMatch below min_confidence: a wrong click is worse than none.
    - Enforces a per-finder budget on model calls (a policy-layer budget hook).
    """

    def __init__(self, model: VisionModelClient,
                 min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                 max_candidates: int = DEFAULT_MAX_CANDIDATES,
                 max_model_calls: int | None = None):
        self.model = model
        self.min_confidence = min_confidence
        self.max_candidates = max_candidates
        self.max_model_calls = max_model_calls
        self.model_calls = 0

    def shortlist(self, intent: str, candidates: Sequence[Candidate]) -> list[Candidate]:
        usable = [c for c in candidates
                  if c.bbox is None or (c.bbox.width > 0 and c.bbox.height > 0)]
        if len(usable) <= self.max_candidates:
            return list(usable)
        ranked = sorted(enumerate(usable),
                        key=lambda ic: (-lexical_score(intent, ic[1]), ic[0]))
        keep = {i for i, _ in ranked[: self.max_candidates]}
        return [c for i, c in enumerate(usable) if i in keep]  # keep page order

    def find(self, intent: str, observation: Observation) -> Match | NoMatch:
        if not intent or not intent.strip():
            return NoMatch("empty intent")
        shortlist = self.shortlist(intent, observation.candidates)
        if not shortlist:
            return NoMatch("no visible candidates on the page")
        if self.max_model_calls is not None and self.model_calls >= self.max_model_calls:
            return NoMatch("model call budget exhausted")
        self.model_calls += 1
        choice = self.model.choose(intent, observation, shortlist)
        if choice.candidate_id is None:
            return NoMatch("model found no matching element",
                           choice.confidence, choice.rationale)
        by_id = {c.id: c for c in shortlist}
        cand = by_id.get(choice.candidate_id)
        if cand is None:
            return NoMatch(f"model chose unknown candidate {choice.candidate_id!r}",
                           choice.confidence, choice.rationale)
        if choice.confidence < self.min_confidence:
            return NoMatch(f"confidence {choice.confidence:.2f} below "
                           f"threshold {self.min_confidence:.2f}",
                           choice.confidence, choice.rationale)
        return Match(cand, choice.confidence, choice.rationale)


# ── Observation from a live page ──────────────────────────────────────────────

# Collects visible interactive elements with an accessible name and bounding
# box, tags each with data-sw-candidate so the chosen one can be re-located
# exactly. Tags are overwritten on every observe().
_COLLECT_JS = """
([attr, limit]) => {
  document.querySelectorAll('[' + attr + ']').forEach(e => e.removeAttribute(attr));
  const sel = 'a[href],button,input:not([type=hidden]),select,textarea,summary,' +
              '[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],' +
              '[role=radio],[role=switch],[role=option],[contenteditable=true],[onclick]';
  const out = [];
  const nameOf = e => {
    const lb = e.getAttribute('aria-labelledby');
    if (lb) {
      const t = lb.split(/\\s+/).map(id => document.getElementById(id))
                 .filter(Boolean).map(n => n.innerText).join(' ').trim();
      if (t) return t;
    }
    if (e.getAttribute('aria-label')) return e.getAttribute('aria-label');
    if (e.labels && e.labels.length) return [...e.labels].map(l => l.innerText).join(' ');
    return e.getAttribute('alt') || e.getAttribute('title') ||
           e.getAttribute('placeholder') || (e.innerText || e.value || '');
  };
  for (const e of document.querySelectorAll(sel)) {
    if (out.length >= limit) break;
    const r = e.getBoundingClientRect();
    const st = getComputedStyle(e);
    if (r.width <= 0 || r.height <= 0 || st.visibility === 'hidden' || st.display === 'none') continue;
    const id = 'c' + out.length;
    e.setAttribute(attr, id);
    const attrs = {};
    for (const k of ['id', 'type', 'name', 'href', 'placeholder']) {
      const v = e.getAttribute(k); if (v) attrs[k] = v.slice(0, 120);
    }
    out.push({
      id, tag: e.tagName.toLowerCase(), role: e.getAttribute('role') || '',
      name: (nameOf(e) || '').trim().replace(/\\s+/g, ' ').slice(0, 120),
      text: (e.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 120),
      bbox: [r.x + window.scrollX, r.y + window.scrollY, r.width, r.height], attrs,
    });
  }
  return out;
}
"""


async def observe(page, screenshot: bool = True, limit: int = 200) -> Observation:
    """Snapshot the page: candidate list (+ screenshot when a model needs it)."""
    raw = await page.evaluate(_COLLECT_JS, [CANDIDATE_ATTR, limit])
    candidates = [
        Candidate(id=r["id"], tag=r["tag"], role=r["role"], name=r["name"],
                  text=r["text"], bbox=BBox(*r["bbox"]), attributes=r["attrs"])
        for r in raw
    ]
    shot = await page.screenshot(full_page=True) if screenshot else None
    vp = page.viewport_size
    return Observation(url=page.url, candidates=candidates, screenshot=shot,
                       viewport=(vp["width"], vp["height"]) if vp else None,
                       title=await page.title())


async def _bbox_of(locator) -> BBox | None:
    try:
        b = await locator.bounding_box(timeout=1000)
    except Exception:
        return None
    return BBox(b["x"], b["y"], b["width"], b["height"]) if b else None


async def _selector_hit(page, selector: str):
    if not selector:
        return None, "no selector"
    try:
        loc = page.locator(selector)
        n = await loc.count()
        if n == 0:
            return None, "selector matched nothing"
        if n > 1:
            return None, f"selector matched {n} elements"
        if not await loc.is_visible():
            return None, "selector matched a hidden element"
        return loc, ""
    except Exception as exc:  # invalid selector, detached frame...
        return None, f"selector error: {exc}"


async def resolve(page, selector: str | None, intent: str | None,
                  finder: IntentFinder | None = None,
                  enabled: bool | None = None,
                  screenshot: bool = True) -> Resolution:
    """Selector first, then the intent finder, then fail safe (no locator)."""
    attempts: list[dict] = []
    loc, why = await _selector_hit(page, selector or "")
    attempts.append({"strategy": "selector", "selector": selector, "ok": loc is not None,
                     "reason": why})
    if loc is not None:
        return Resolution("selector", loc, attempts=attempts)

    if enabled is None:
        enabled = intent_locator_enabled()
    if not enabled or finder is None or not intent:
        reason = ("intent locator disabled" if not enabled
                  else "no intent finder configured" if finder is None
                  else "no intent given")
        attempts.append({"strategy": "intent", "ok": False, "reason": reason})
        return Resolution("none", reason=f"{why}; {reason}", attempts=attempts)

    obs = await observe(page, screenshot=screenshot)
    result = finder.find(intent, obs)
    if isinstance(result, NoMatch):
        attempts.append({"strategy": "intent", "ok": False, "reason": result.reason,
                         "confidence": result.confidence})
        return Resolution("none", reason=f"{why}; {result.reason}", attempts=attempts)
    attempts.append({"strategy": "intent", "ok": True, "candidate": result.candidate.id,
                     "confidence": result.confidence, "rationale": result.rationale})
    loc = page.locator(f'[{CANDIDATE_ATTR}="{result.candidate.id}"]')
    return Resolution("intent", loc, match=result, attempts=attempts)


async def resolve_stable(page, selector: str | None, intent: str | None,
                         finder: IntentFinder | None = None,
                         enabled: bool | None = None,
                         settle_ms: int = 250, max_reresolves: int = 2,
                         tolerance: float = 2.0) -> Resolution:
    """resolve(), then confirm the element stayed put before the caller acts.

    If it moved or detached during settle_ms (late-loading banner, re-render,
    A/B swap), resolve again, up to max_reresolves times. Returns the last
    Resolution; check .found before acting.
    """
    res = await resolve(page, selector, intent, finder, enabled)
    for i in range(max_reresolves + 1):
        if not res.found:
            return res
        before = await _bbox_of(res.locator)
        await asyncio.sleep(settle_ms / 1000)
        after = await _bbox_of(res.locator)
        if before is not None and after is not None and not after.moved_from(before, tolerance):
            return res
        if i == max_reresolves:
            break
        prior = res.attempts
        res = await resolve(page, selector, intent, finder, enabled)
        res.attempts = prior + [{"strategy": "layout-shift", "ok": False,
                                 "reason": "element moved or detached; re-resolved"}] + res.attempts
    res.attempts.append({"strategy": "layout-shift", "ok": False,
                         "reason": "element never settled"})
    return Resolution("none", reason="element never settled", attempts=res.attempts)


def resolution_log(res: Resolution) -> dict:
    """JSON-safe summary for the crawler's structured log / artifacts."""
    out = {"strategy": res.strategy, "found": res.found, "reason": res.reason,
           "attempts": res.attempts}
    if res.match:
        out["match"] = {"candidate": asdict(res.match.candidate),
                        "confidence": res.match.confidence,
                        "rationale": res.match.rationale}
    return out
