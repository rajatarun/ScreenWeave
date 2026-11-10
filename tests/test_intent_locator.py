"""Intent locator: offline unit tests with the deterministic fake model.

No browser, no network, no AWS. The Bedrock adapter is exercised against a
stub client that records the request and returns a canned reply.
"""
from __future__ import annotations

import io
import json
import pathlib
import sys

import pytest
from botocore.exceptions import ClientError

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "crawler"))

import intent_locator as il  # noqa: E402


def cand(cid, name, role="button", tag="button", text=None, bbox=(0, 0, 80, 30), **attrs):
    return il.Candidate(id=cid, tag=tag, role=role, name=name,
                        text=name if text is None else text,
                        bbox=il.BBox(*bbox) if bbox else None, attributes=attrs)


def obs(*cands, screenshot=b"\x89PNG fake"):
    return il.Observation(url="https://shop.example/cart", candidates=list(cands),
                          screenshot=screenshot, title="Cart")


CART = obs(cand("c0", "Continue shopping"), cand("c1", "Empty cart"),
           cand("c2", "Checkout", text="Proceed to checkout"))


# ── Flag ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("", False), ("off", False), ("0", False), ("false", False),
    ("on", True), ("1", True), ("TRUE", True), ("yes", True),
])
def test_flag_is_opt_in(value, expected):
    assert il.intent_locator_enabled({il.FLAG_ENV: value}) is expected


def test_flag_defaults_off():
    assert il.intent_locator_enabled({}) is False


# ── Fake model + finder ───────────────────────────────────────────────────────

def test_finder_picks_the_matching_candidate():
    finder = il.IntentFinder(il.FakeVisionModel())
    result = finder.find("the checkout button", CART)
    assert isinstance(result, il.Match)
    assert result.candidate.id == "c2"
    assert result.confidence == 1.0
    assert result.rationale


def test_finder_returns_no_match_when_nothing_fits():
    result = il.IntentFinder(il.FakeVisionModel()).find("delete my account", CART)
    assert isinstance(result, il.NoMatch)


def test_below_threshold_is_no_match():
    model = il.FakeVisionModel(fixed=il.ModelChoice("c2", 0.4, "maybe"))
    result = il.IntentFinder(model, min_confidence=0.6).find("checkout", CART)
    assert isinstance(result, il.NoMatch)
    assert "below threshold" in result.reason
    assert result.confidence == 0.4


def test_invented_candidate_id_is_rejected():
    model = il.FakeVisionModel(fixed=il.ModelChoice("c99", 0.99, "trust me"))
    result = il.IntentFinder(model).find("checkout", CART)
    assert isinstance(result, il.NoMatch)
    assert "unknown candidate" in result.reason


def test_empty_intent_and_empty_page_never_call_the_model():
    model = il.FakeVisionModel()
    finder = il.IntentFinder(model)
    assert isinstance(finder.find("  ", CART), il.NoMatch)
    assert isinstance(finder.find("checkout", obs()), il.NoMatch)
    assert model.calls == []


def test_zero_size_candidates_are_not_offered():
    model = il.FakeVisionModel()
    page = obs(cand("c0", "Checkout", bbox=(0, 0, 0, 0)), cand("c1", "Checkout"))
    result = il.IntentFinder(model).find("checkout", page)
    assert result.candidate.id == "c1"
    assert model.calls[0]["candidate_ids"] == ["c1"]


def test_shortlist_caps_candidates_and_keeps_the_likely_ones_in_page_order():
    noise = [cand(f"n{i}", f"Footer link {i}", role="link", tag="a") for i in range(60)]
    page = obs(*noise[:30], cand("target", "Checkout"), *noise[30:])
    model = il.FakeVisionModel()
    finder = il.IntentFinder(model, max_candidates=10)
    result = finder.find("checkout", page)
    sent = model.calls[0]["candidate_ids"]
    assert len(sent) == 10 and "target" in sent
    assert result.candidate.id == "target"
    order = [c.id for c in page.candidates]
    assert sent == sorted(sent, key=order.index)


def test_model_call_budget_is_enforced():
    model = il.FakeVisionModel()
    finder = il.IntentFinder(model, max_model_calls=1)
    assert isinstance(finder.find("checkout", CART), il.Match)
    second = finder.find("checkout", CART)
    assert isinstance(second, il.NoMatch) and "budget" in second.reason
    assert len(model.calls) == 1


def test_bbox_movement():
    a = il.BBox(10, 10, 80, 30)
    assert not il.BBox(11, 10, 80, 30).moved_from(a)
    assert il.BBox(10, 310, 80, 30).moved_from(a)
    assert a.center == (50, 25)


# ── Model reply parsing ───────────────────────────────────────────────────────

@pytest.mark.parametrize("text,cid,conf", [
    ('{"candidate_id": "c2", "confidence": 0.91, "rationale": "label says Checkout"}', "c2", 0.91),
    ('```json\n{"candidate_id": "c1", "confidence": 0.7, "rationale": "x"}\n```', "c1", 0.7),
    ('Sure! {"candidate_id": null, "confidence": 0.2, "rationale": "none"}', None, 0.2),
    ('{"candidate_id": "c0", "confidence": 7}', "c0", 1.0),
    ('{"candidate_id": "c0", "confidence": "high"}', "c0", 0.0),
    ("I cannot see the page", None, 0.0),
    ("{not json}", None, 0.0),
])
def test_parse_model_json(text, cid, conf):
    choice = il.parse_model_json(text)
    assert choice.candidate_id == cid
    assert choice.confidence == conf


# ── Bedrock adapter (stub client, no AWS) ─────────────────────────────────────

class StubBedrock:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def invoke_model(self, **kwargs):
        self.requests.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        payload = {"content": [{"type": "text", "text": reply}]}
        return {"body": io.BytesIO(json.dumps(payload).encode())}


def _throttle():
    return ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                       "InvokeModel")


def test_bedrock_request_matches_the_visual_qa_shape():
    stub = StubBedrock(['{"candidate_id": "c2", "confidence": 0.9, "rationale": "ok"}'])
    model = il.BedrockVisionModel(model_id="test-model", client=stub)
    result = il.IntentFinder(model).find("checkout", CART)
    assert isinstance(result, il.Match) and result.candidate.id == "c2"

    req = stub.requests[0]
    assert req["modelId"] == "test-model"
    assert req["contentType"] == "application/json"
    body = json.loads(req["body"])
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    text = content[1]["text"]
    assert "Intent: checkout" in text
    listed = json.loads(text.split("Candidates:\n", 1)[1])
    assert [c["id"] for c in listed] == ["c0", "c1", "c2"]
    assert listed[2]["bbox"] == [0, 0, 80, 30]


def test_bedrock_without_screenshot_sends_text_only():
    stub = StubBedrock(['{"candidate_id": null, "confidence": 0, "rationale": "none"}'])
    model = il.BedrockVisionModel(client=stub)
    model.choose("checkout", obs(cand("c0", "Checkout"), screenshot=None),
                 [cand("c0", "Checkout")])
    content = json.loads(stub.requests[0]["body"])["messages"][0]["content"]
    assert [c["type"] for c in content] == ["text"]


def test_bedrock_retries_throttling_then_succeeds():
    slept = []
    stub = StubBedrock([_throttle(), _throttle(),
                        '{"candidate_id": "c2", "confidence": 0.8, "rationale": "ok"}'])
    model = il.BedrockVisionModel(client=stub, sleep=slept.append)
    choice = model.choose("checkout", CART, CART.candidates)
    assert choice.candidate_id == "c2"
    assert slept == [2, 4]


def test_bedrock_gives_up_after_backoff():
    stub = StubBedrock([_throttle()] * 4)
    model = il.BedrockVisionModel(client=stub, sleep=lambda s: None)
    with pytest.raises(RuntimeError, match="throttling"):
        model.choose("checkout", CART, CART.candidates)


def test_bedrock_other_errors_raise():
    err = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "InvokeModel")
    model = il.BedrockVisionModel(client=StubBedrock([err]), sleep=lambda s: None)
    with pytest.raises(ClientError):
        model.choose("checkout", CART, CART.candidates)


def test_module_import_needs_no_playwright_or_aws():
    assert "playwright" not in il.__dict__
    assert il.BedrockVisionModel()._client is None  # no boto3 client until first call
