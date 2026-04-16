"""
ScreenWeave — Image Classifier

Implements Design Principles 1 & 4 from the ScreenWeave cost-efficiency architecture:
  - Principle 1: Stratified Model Routing (cheap local heuristics decide which Claude model to use)
  - Principle 4: Progressive Enhancement (three quality tiers: heuristic → Haiku → Sonnet)

All logic uses only Pillow (already a Lambda dependency) — no extra packages, no API calls.
"""

import io
import math
import logging
from typing import Literal
from PIL import Image, ImageFilter

logger = logging.getLogger(__name__)

# ── Tier thresholds ───────────────────────────────────────────────────────────
# Complexity score range: 0.0 (trivially simple) → 1.0 (maximally complex)
#
# Tier 1 "heuristic"  (<  HAIKU_THRESHOLD) : local assessment only, no Bedrock call
# Tier 2 "haiku"      (<  SONNET_THRESHOLD): Claude 3.5 Haiku, ~2–3× cheaper than Sonnet
# Tier 3 "sonnet"     (>= SONNET_THRESHOLD): Claude Sonnet 4, full multi-modal reasoning

HEURISTIC_THRESHOLD = 0.20
SONNET_THRESHOLD    = 0.60

# Weight of each signal in the composite score
_W_ENTROPY      = 0.35
_W_COLOR        = 0.25
_W_EDGES        = 0.25
_W_INTERACTIVE  = 0.15

# Colour-dominance threshold for UI-vs-photographic detection
# UI screenshots typically have ≥25% of pixels in the top-10 colours (large flat regions)
_UI_DOMINANT_THRESHOLD = 0.25


def compute_complexity_score(image_bytes: bytes, state_metadata: dict) -> float:
    """
    Score how visually and structurally complex a screenshot is.

    Signals (all derived locally from Pillow + metadata):
      1. Entropy          — information density of the greyscale histogram
      2. Color diversity  — number of distinct RGB colours (normalised)
      3. Edge density     — fraction of high-gradient pixels (structural complexity)
      4. Interactive count— number of interactive elements in the state metadata

    Returns a float in [0.0, 1.0].  Higher = more complex = needs Sonnet.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
    except Exception as exc:
        logger.warning("Cannot decode image for complexity scoring: %s", exc)
        return 0.5  # default to mid-range (Haiku) on decode failure

    # ── 1. Entropy of the greyscale histogram ─────────────────────────────────
    gray = img.convert("L")
    hist = gray.histogram()          # 256-bucket histogram
    total = sum(hist) or 1
    entropy = -sum(
        (p / total) * math.log2(p / total)
        for p in hist if p > 0
    )
    entropy_score = min(entropy / 8.0, 1.0)  # max possible ≈ 8 bits for 8-bit images

    # ── 2. Color diversity ────────────────────────────────────────────────────
    try:
        rgb = img.convert("RGB")
        colors = rgb.getcolors(maxcolors=5000)
        color_score = min(len(colors) / 300.0, 1.0) if colors else 1.0
    except Exception:
        color_score = 0.5

    # ── 3. Edge density (structural / layout complexity) ─────────────────────
    try:
        edges = gray.filter(ImageFilter.FIND_EDGES)
        edge_pixels = list(edges.getdata())
        n_pixels = len(edge_pixels) or 1
        edge_density = sum(1 for p in edge_pixels if p > 30) / n_pixels
        edge_score = min(edge_density * 5.0, 1.0)  # scale: 20% edge pixels → 1.0
    except Exception:
        edge_score = 0.5

    # ── 4. Interactive element count (metadata signal) ────────────────────────
    interactive = state_metadata.get("interactive_elements") or []
    interactive_score = min(len(interactive) / 15.0, 1.0)

    composite = (
        entropy_score     * _W_ENTROPY +
        color_score       * _W_COLOR +
        edge_score        * _W_EDGES +
        interactive_score * _W_INTERACTIVE
    )

    logger.debug(
        "Complexity: %.3f (entropy=%.3f, color=%.3f, edges=%.3f, interactive=%.3f)",
        composite, entropy_score, color_score, edge_score, interactive_score,
    )
    return round(composite, 4)


def select_tier(complexity: float) -> Literal["heuristic", "haiku", "sonnet"]:
    """
    Map a complexity score to one of three processing tiers.

    Tier 1 "heuristic"  — score < 0.20: simple structural layout, skip Bedrock entirely
    Tier 2 "haiku"      — score < 0.60: moderate complexity, use Claude 3.5 Haiku
    Tier 3 "sonnet"     — score ≥ 0.60: high complexity, use Claude Sonnet 4

    Expected distribution in typical web crawls (from ScreenWeave design docs):
      ~20% heuristic | ~40% haiku | ~40% sonnet
    """
    if complexity < HEURISTIC_THRESHOLD:
        return "heuristic"
    if complexity < SONNET_THRESHOLD:
        return "haiku"
    return "sonnet"


def is_ui_content(img: Image.Image) -> bool:
    """
    Return True if the image looks like a UI screenshot (vs photographic content).

    UI screenshots have large flat-colour regions (navigation bars, card backgrounds,
    form fields) that push many pixels into a small set of dominant colours.
    Photographic images spread pixels more evenly across the colour space.

    This is used by the preprocessor to decide between lossless PNG (UI, preserves
    text crispness) and JPEG at 80% quality (photographic, much smaller).
    """
    try:
        rgb = img.convert("RGB")
        colors = rgb.getcolors(maxcolors=10000)
        if colors is None:
            # > 10,000 unique colours → very likely photographic
            return False
        total = (img.width * img.height) or 1
        top10 = sorted(colors, key=lambda x: -x[0])[:10]
        dominant_pct = sum(c[0] for c in top10) / total
        return dominant_pct >= _UI_DOMINANT_THRESHOLD
    except Exception:
        return True  # Default to UI (PNG) — safer for text preservation


def heuristic_assessment(state: dict) -> str:
    """
    Generate a lightweight local QA summary for a Tier-1 (simple) state.

    Called instead of a Bedrock invocation — produces a structured plain-text
    description that is injected into the multi-turn conversation as a
    synthetic assistant turn, preserving context continuity.

    Assessment covers: URL, trigger action, interactive element inventory,
    and a visible-text excerpt — all from existing metadata, no image needed.
    """
    state_id     = state.get("state_id", "unknown")
    url          = state.get("url", "")
    trigger      = state.get("trigger_label") or "navigation"
    text_preview = (state.get("visible_text_preview") or "")[:300]
    interactive  = state.get("interactive_elements") or []

    lines = [
        f"[HEURISTIC TIER] State {state_id} assessed locally (low visual complexity).",
        f"URL: {url}",
        f"Trigger: {trigger}",
        f"Interactive elements detected: {len(interactive)}"
        + (f" — {', '.join(str(e) for e in interactive[:5])}" if interactive else ""),
    ]
    if text_preview:
        lines.append(f"Visible text excerpt: {text_preview}")
    lines.append(
        "No visual anomalies flagged at heuristic tier. "
        "Structural layout appears standard for this state type."
    )
    return "\n".join(lines)
