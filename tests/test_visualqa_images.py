"""The Visual QA worker's image functions run on the pinned Pillow.

Pillow was 11.2.1, with advisories fixed only in 12.x and 13.0.0, and this
worker decodes screenshots of whatever site was crawled. The upgrade to 13.0.0
was checked by running these functions on both versions with identical
results; this test keeps the next upgrade honest.
"""
from __future__ import annotations

import io
import pathlib
import sys

from PIL import Image, ImageDraw

WORKER = pathlib.Path(__file__).resolve().parents[1] / "src" / "lambda" / "visualQAWorker"
sys.path.insert(0, str(WORKER))

import cache  # noqa: E402
import image_classifier  # noqa: E402
import preprocessor  # noqa: E402


def _screenshot(width=2400, height=1600) -> bytes:
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    for x in range(0, width, 120):
        draw.rectangle([x, 100, x + 80, 160], outline="black")
        draw.text((x + 5, 120), "Btn", fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_hash_classify_and_preprocess_a_screenshot():
    data = _screenshot()
    h = cache.compute_phash(data)
    assert len(h) == 16 and cache.hamming_distance(h, h) == 0
    score = image_classifier.compute_complexity_score(data, {"interactive_elements": [{}] * 5})
    assert 0.0 <= score <= 1.0
    assert image_classifier.select_tier(score) in ("heuristic", "haiku", "sonnet")
    assert image_classifier.is_ui_content(Image.open(io.BytesIO(data)))
    out, media_type = preprocessor.preprocess(data)
    assert media_type in ("image/png", "image/jpeg") and out
    assert Image.open(io.BytesIO(out)).size[0] <= 2400
    assert preprocessor.estimate_tokens(out) > 0


def test_the_pinned_pillow_is_the_audited_one():
    import PIL
    req = (WORKER / "requirements.txt").read_text()
    assert f"Pillow=={PIL.__version__}" in req or "Pillow==" not in req
