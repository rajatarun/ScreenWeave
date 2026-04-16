"""
ScreenWeave — Image Preprocessor

Implements Design Principle 5: Infrastructure-Level Optimizations.

Applies three optimisations to every screenshot before it is sent to Bedrock:
  1. Dimension capping — max 1024 px wide, with existing short/long-edge constraints
  2. Format selection — PNG for UI/text content (lossless), JPEG 80% for photographic
  3. Token estimation — pre-flight cost check using Claude's ~W×H/750 formula

Format selection guidance from the ScreenWeave architecture doc:
  • PNG  — lossless; preserves text crispness; best for UI screenshots
  • JPEG — lossy at 80% quality; 30–40% smaller than PNG; fine for photographic content
  • WebP — not used (Lambda runtime PIL build may lack encoder support)

Expected savings: 15% overall token reduction from format + size optimisation.
"""

import io
import logging
import math

from PIL import Image

from image_classifier import is_ui_content

logger = logging.getLogger(__name__)

# ── Dimension constraints ──────────────────────────────────────────────────────

# New cap: max 1024 px along the width axis.
# The existing worker used a 512 px short-edge target; we keep that as a secondary
# constraint and add width capping as the primary one so wide desktop screenshots
# are reduced more aggressively.
MAX_WIDTH      = 1024
MAX_SHORT_EDGE = 512
MAX_LONG_EDGE  = 1568   # Bedrock hard limit for multi-image requests

# ── JPEG quality ───────────────────────────────────────────────────────────────
JPEG_QUALITY = 80

# ── Token estimation ───────────────────────────────────────────────────────────
# Claude's approximate formula: tokens ≈ ceil(width × height / 750)
# This matches observed token counts for typical screenshots to within ~10%.
_TOKENS_PER_PIXEL_DIVISOR = 750


def preprocess(data: bytes, force_png: bool = False) -> tuple[bytes, str]:
    """
    Resize and compress a screenshot.

    Applies dimension constraints then chooses the optimal format:
      • PNG  if the image looks like a UI screenshot (or force_png=True)
      • JPEG at 80% quality otherwise (photographic content)

    Returns (processed_bytes, media_type) where media_type is
    "image/png" or "image/jpeg".

    No-op dimensions: if all constraints are already satisfied and the
    detected format matches the target, the image is re-encoded once
    (to apply any format conversion) but not resized.
    """
    img = Image.open(io.BytesIO(data))
    w, h = img.size

    # ── Compute the most restrictive scale factor ──────────────────────────────
    scale = 1.0

    # Constraint 1: max width
    if w > MAX_WIDTH:
        scale = min(scale, MAX_WIDTH / w)

    # Constraint 2: short-edge target (512 px)
    short_edge = min(w, h)
    if short_edge * scale > MAX_SHORT_EDGE:
        scale = min(scale, MAX_SHORT_EDGE / short_edge)

    # Constraint 3: long-edge hard cap (1568 px — Bedrock limit)
    long_edge = max(w, h)
    if long_edge * scale > MAX_LONG_EDGE:
        scale = min(scale, MAX_LONG_EDGE / long_edge)

    if scale < 1.0:
        new_w = max(1, int(w * scale))
        new_h = max(1, int(h * scale))
        logger.info("Resizing screenshot %dx%d → %dx%d (scale=%.3f)", w, h, new_w, new_h, scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)

    # ── Format selection ───────────────────────────────────────────────────────
    use_png = force_png or is_ui_content(img)

    buf = io.BytesIO()
    if use_png:
        img.save(buf, format="PNG", optimize=True)
        media_type = "image/png"
    else:
        # JPEG does not support alpha or palette modes — convert first
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        media_type = "image/jpeg"

    processed = buf.getvalue()
    logger.debug(
        "Preprocessed: %d → %d bytes (%s)",
        len(data), len(processed), media_type,
    )
    return processed, media_type


def estimate_tokens(image_bytes: bytes) -> int:
    """
    Estimate the number of Claude vision tokens for a processed image.

    Uses Claude's approximate formula: tokens ≈ ceil(width × height / 750).
    This is accurate to within ~10% for typical screenshots.

    Intended for pre-flight cost checks: if the estimate exceeds the
    MAX_TOKENS_PER_IMAGE threshold the caller can choose to skip the image
    or downscale further rather than paying for an expensive invocation.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        return math.ceil(w * h / _TOKENS_PER_PIXEL_DIVISOR)
    except Exception as exc:
        logger.warning("Token estimation failed: %s", exc)
        return 0
