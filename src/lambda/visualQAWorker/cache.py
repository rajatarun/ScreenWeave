"""
ScreenWeave — Perceptual Hash Cache

Implements Design Principle 3: Smart Caching and Fingerprinting.

Screenshots that look identical (same dashboard state, same UI template) share
the same perceptual hash regardless of timestamp or session.  Cached
interpretations are stored in DynamoDB with a 72-hour TTL and reused directly
rather than re-sending the image to Bedrock.

Expected cache hit rate in stable systems: 40–60%.
Each cache hit saves one full Bedrock image invocation.

Algorithm: Difference Hash (dHash) — fast, pure-Pillow, no extra dependencies.
  1. Convert image to greyscale, resize to 9×8
  2. For each row, compare adjacent pixel pairs (8 comparisons × 8 rows = 64 bits)
  3. Pack bits into a 16-character hex string

Two images are considered "identical" when their hashes match exactly.
(Fuzzy matching via Hamming distance is available but not used in the hot path
to keep DynamoDB lookups O(1).)
"""

import io
import logging
import time
from datetime import datetime, timezone

from PIL import Image

logger = logging.getLogger(__name__)

# Cache TTL — 72 hours in seconds.
# Longer TTL favours hit rate; shorter favours freshness for rapidly changing dashboards.
CACHE_TTL_SECONDS = 72 * 3600

# dHash image dimensions: 9 columns × 8 rows → 64 comparison bits
_DHASH_COLS = 9
_DHASH_ROWS = 8


def compute_phash(image_bytes: bytes) -> str:
    """
    Compute a 64-bit difference hash (dHash) for an image.

    Returns a 16-character lowercase hex string.
    Identical images always produce the same hash; visually similar images
    tend to produce hashes with small Hamming distance.

    Uses only Pillow — no imagehash library required.
    """
    img = Image.open(io.BytesIO(image_bytes))
    # Resize to (_DHASH_COLS × _DHASH_ROWS) greyscale — captures gross structure,
    # ignores minor colour/compression differences
    img = img.convert("L").resize((_DHASH_COLS, _DHASH_ROWS), Image.LANCZOS)
    pixels = list(img.getdata())  # _DHASH_COLS * _DHASH_ROWS values

    # Generate 64 bits: 1 if left pixel > right pixel, else 0
    bits: list[int] = []
    for row in range(_DHASH_ROWS):
        for col in range(_DHASH_COLS - 1):  # 8 comparisons per row
            left  = pixels[row * _DHASH_COLS + col]
            right = pixels[row * _DHASH_COLS + col + 1]
            bits.append(1 if left > right else 0)

    # Pack 64 bits into a 16-char hex string (MSB first)
    value = 0
    for bit in bits:
        value = (value << 1) | bit
    return format(value, "016x")


def hamming_distance(hash1: str, hash2: str) -> int:
    """
    Count the number of differing bits between two hex hashes.

    Useful for fuzzy matching: distance 0 = identical, ≤ 10 = visually similar.
    Not used in the primary lookup path (which requires exact match) but
    available for offline cache analysis.
    """
    n1 = int(hash1, 16)
    n2 = int(hash2, 16)
    return bin(n1 ^ n2).count("1")


def lookup(phash: str, dynamo, table_name: str) -> str | None:
    """
    Look up a cached interpretation by perceptual hash.

    Returns the cached interpretation string if found and not expired,
    or None on a cache miss or any DynamoDB error (fail-open: always
    proceed with Bedrock on any cache failure).

    DynamoDB TTL expiry is handled by AWS automatically; items past
    their TTL may still appear briefly before deletion, so the caller
    should treat any returned string as valid.
    """
    if not table_name:
        return None
    try:
        resp = dynamo.get_item(
            TableName=table_name,
            Key={"phash": {"S": phash}},
            ProjectionExpression="interpretation",
        )
        item = resp.get("Item")
        if item:
            interpretation = item.get("interpretation", {}).get("S")
            if interpretation:
                logger.info("Cache HIT for phash %.8s…", phash)
                return interpretation
    except Exception as exc:
        logger.warning("Cache lookup failed (phash=%.8s…): %s", phash, exc)
    return None


def store(phash: str, interpretation: str, dynamo, table_name: str) -> None:
    """
    Store a Bedrock interpretation in the DynamoDB cache.

    Sets a TTL of CACHE_TTL_SECONDS from now so AWS automatically
    expires old entries.  Errors are logged and silently swallowed —
    a cache write failure should never abort the QA pipeline.
    """
    if not table_name:
        return
    if not interpretation:
        return
    try:
        ttl = int(time.time()) + CACHE_TTL_SECONDS
        dynamo.put_item(
            TableName=table_name,
            Item={
                "phash":          {"S": phash},
                "interpretation": {"S": interpretation},
                "ttl":            {"N": str(ttl)},
                "created_at":     {"S": datetime.now(timezone.utc).isoformat()},
            },
        )
        logger.debug("Cache STORE for phash %.8s… (TTL +%dh)", phash, CACHE_TTL_SECONDS // 3600)
    except Exception as exc:
        logger.warning("Cache store failed (phash=%.8s…): %s", phash, exc)
