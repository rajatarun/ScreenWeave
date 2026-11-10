"""QA agent: classification, plan-first runs, sessions, reruns. Offline only.

Fake executor and in-memory store; the AWS store runs against stub clients.
No browser, no model, no AWS.
"""
from __future__ import annotations

import asyncio
import io
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "crawler"))
sys.path.insert(0, str(ROOT / "src" / "lambda" / "visualQAWorker"))

import qa_agent as qa  # noqa: E402
import signoff_chain as sc  # noqa: E402

CONTRACT = json.loads((ROOT / "contracts" / "qa_request_classification.json").read_text())
URL = "https://shop.example.com/"

STATES = [
    {"state_id": "state_0001", "url": URL, "title": "Shop", "trigger_action": "navigation",
     "headings": [{"tag": "h1", "text": "Welcome to Shop"}],
     "interactive_elements": ["Add to cart", "Checkout", "Newsletter"]},
    {"state_id": "state_0002", "url": URL + "pricing", "title": "Pricing", "trigger_action": "navigation",
     "headings": [{"tag": "h1", "text": "Pricing"}], "interactive_elements": ["Monthly", "Yearly"]},
    {"state_id": "state_0003", "url": URL, "title": "Shop", "trigger_action": "click",
     "interactive_elements": ["Checkout"]},
]


# ── Shared classification rule ────────────────────────────────────────────────

def test_word_lists_match_the_contract():
    assert qa.GENERIC_WORDS == frozenset(CONTRACT["generic_words"])
    assert qa.TEST_VERBS == frozenset(CONTRACT["test_verbs"])
    assert qa.ACTION_VERBS == frozenset(CONTRACT["action_verbs"])


@pytest.mark.parametrize("case", CONTRACT["cases"], ids=lambda c: c["text"] or "<empty>")
def test_contract_cases(case):
    c = qa.classify_request(case["text"])
    assert (c.kind, c.rule) == (case["kind"], case["rule"])


def test_ambiguous_carries_focus_words():
    c = qa.classify_request("the pricing table looks off on mobile")
    assert c.ambiguous and "pricing" in c.focus and "mobile" in c.focus


class FakeClassifier:
    def __init__(self, kind, conf):
        self.kind, self.conf, self.calls = kind, conf, 0

    def classify(self, text):
        self.calls += 1
        return qa.Classification(self.kind, "model", confidence=self.conf)


def test_model_only_consulted_for_ambiguous_and_only_trusted_when_confident():
    m = FakeClassifier("specific", 0.9)
    assert qa.classify_with_optional_model("QA this site", m).rule == "generic"
    assert m.calls == 0
    assert qa.classify_with_optional_model("focus on accessibility", m).kind == "specific"
    low = FakeClassifier("specific", 0.4)
    assert qa.classify_with_optional_model("focus on accessibility", low).rule == "ambiguous"


# ── Instruction translation ───────────────────────────────────────────────────

def ctx(**kw):
    base = dict(run_id="run-1", attempt=1, url=URL, viewport={"width": 1280, "height": 800},
                instruction="", messages=[], adjustments={}, states=STATES)
    base.update(kw)
    return qa.QAContext(**base)


def test_rule_translator_turns_steps_into_a_case():
    tc = qa.RuleInstructionTranslator().translate(
        'Click Checkout, then fill "jane@example.com" into the email field, then verify "Order placed"', ctx())
    assert [s.action for s in tc.steps] == ["navigate", "click", "fill", "assert_text"]
    assert tc.steps[1].intent == "Checkout"
    assert tc.steps[2].intent == "email" and tc.steps[2].value == "jane@example.com"
    assert tc.status == "ready" and tc.source == "user-instruction" and tc.priority == "P0"


def test_rule_translator_marks_unparsable_conditions_for_clarification():
    tc = qa.RuleInstructionTranslator().translate("test checkout with an expired card", ctx())
    assert tc.status == "needs-clarification"
    assert any("with an expired card" in n for n in tc.notes)
    assert [s.action for s in tc.steps] == ["navigate", "click", "assert_no_error"]


# ── Generation ────────────────────────────────────────────────────────────────

def test_heuristic_generator_cases_have_required_fields_and_sources():
    cases = qa.HeuristicTestCaseGenerator().generate(
        ctx(product_description="A shop with a fast checkout"), 20)
    assert cases[0].source == "product-description" and cases[0].priority == "P0"
    sources = {c.source for c in cases}
    assert {"product-description", "page-content", "crawl-graph"} <= sources
    for c in cases:
        assert c.title and c.steps and c.expected and c.priority in qa.PRIORITIES
        assert c.source in qa.CASE_SOURCES
    assert len(qa.HeuristicTestCaseGenerator().generate(ctx(), 3)) == 3


def test_generator_puts_prior_failures_first():
    prior = qa.Plan("run-1", 1, "planned", {}, [
        qa.TestCase("TC-1", "Page loads: /", [qa.Step("navigate", value=URL)], "ok", "P1", "url"),
        qa.TestCase("TC-2", "Control responds: Checkout", [qa.Step("click", intent="Checkout")], "ok", "P2", "crawl-graph")])
    c = ctx(attempt=2, prior_plan=prior,
            prior_results={"cases": [{"case_id": "TC-2", "status": "failed"}]})
    cases = qa.HeuristicTestCaseGenerator().generate(c, 10)
    assert cases[0].title == "Re-test: Control responds: Checkout" and cases[0].source == "prior-session"


def test_model_output_is_validated():
    data = {"cases": [
        {"title": "Good", "priority": "P0", "source": "crawl-graph", "expected": "x",
         "steps": [{"action": "navigate", "value": URL}, {"action": "rm -rf", "value": "/"}]},
        {"title": "No steps", "steps": [{"action": "explode"}]},
        {"steps": [{"action": "navigate"}]},
        {"title": "Odd fields", "priority": "urgent", "source": "vibes", "steps": [{"action": "click", "intent": "Buy"}]},
    ]}
    cases = qa.cases_from_json(data, "model", 10, 5)
    assert [c.title for c in cases] == ["Good", "Odd fields"]
    assert [s.action for s in cases[0].steps] == ["navigate"]
    assert cases[1].priority == "P1" and cases[1].source == "model"


class StubBedrock:
    def __init__(self, reply):
        self.reply, self.requests = reply, []

    def invoke_model(self, **kw):
        self.requests.append(kw)
        return {"body": io.BytesIO(json.dumps({"content": [{"text": self.reply}]}).encode())}


def test_bedrock_generator_request_shape_and_parse():
    stub = StubBedrock('```json\n{"cases": [{"title": "Cart", "priority": "P0", "source": "crawl-graph", '
                       '"expected": "item added", "steps": [{"action": "click", "intent": "Add to cart"}]}]}\n```')
    gen = qa.BedrockTestCaseGenerator(qa.BedrockJson(model_id="m", client=stub))
    cases = gen.generate(ctx(product_description="shop"), 5)
    assert cases[0].title == "Cart" and cases[0].source == "crawl-graph"
    body = json.loads(stub.requests[0]["body"])
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    sent = json.loads(body["messages"][0]["content"][0]["text"])
    assert sent["url"] == URL and sent["pages"][0]["controls"][:2] == ["Add to cart", "Checkout"]


def test_bedrock_translator_and_classifier():
    stub = StubBedrock('{"title": "x", "expected": "error shown", "steps": ['
                       '{"action": "click", "intent": "Checkout"}, {"action": "assert_text", "value": "expired"}]}')
    tc = qa.BedrockInstructionTranslator(qa.BedrockJson(client=stub)).translate(
        "test checkout with an expired card", ctx())
    assert tc.id == "TC-1" and tc.source == "user-instruction" and tc.steps[0].action == "navigate"
    assert tc.status == "ready"
    none = qa.BedrockInstructionTranslator(qa.BedrockJson(client=StubBedrock(
        '{"title": "x", "steps": [], "missing": "a test card number"}'))).translate("pay with crypto", ctx())
    assert none.status == "needs-clarification" and "test card" in none.notes[0]
    cls = qa.BedrockRequestClassifier(qa.BedrockJson(client=StubBedrock(
        '{"kind": "specific", "confidence": 0.8}'))).classify("focus on accessibility")
    assert cls.kind == "specific" and cls.confidence == 0.8


# ── Policy ────────────────────────────────────────────────────────────────────

def test_policy_caps_the_plan():
    pol = qa.QAPolicy.for_url(URL, max_cases=2, max_steps_per_case=2, max_total_steps=3)
    plan = qa.Plan("r", 1, "planned", {}, [
        qa.TestCase(f"TC-{i}", f"t{i}", [qa.Step("assert_no_error")] * 3, "x") for i in range(1, 5)])
    notes = pol.check_plan(plan)
    assert [c.id for c in plan.cases] == ["TC-1"]
    assert all(len(c.steps) <= 2 for c in plan.cases)
    assert any("max_cases" in n for n in notes) and any("max_total_steps" in n for n in notes)


@pytest.mark.parametrize("step,verdict", [
    (qa.Step("navigate", value="https://shop.example.com/cart"), "allow"),
    (qa.Step("navigate", value="https://cdn.shop.example.com/x"), "allow"),
    (qa.Step("navigate", value="https://evil.example.net/"), "deny"),
    (qa.Step("navigate", value="javascript:alert(1)"), "deny"),
    (qa.Step("click", intent="Add to cart"), "allow"),
    (qa.Step("click", intent="Place order"), "deny"),
    (qa.Step("click", intent="Delete account"), "deny"),
    (qa.Step("fill", intent="email", value="a@b.c"), "allow"),
    (qa.Step("fill", intent="password", value="hunter2"), "deny"),
    (qa.Step("fill", intent="card number", value="4000000000000069"), "deny"),
    (qa.Step("teleport"), "deny"),
])
def test_policy_actions(step, verdict):
    pol = qa.QAPolicy.for_url(URL)
    assert pol.check_action(step, URL, 0, 0).verdict == verdict


def test_policy_allows_operator_approved_test_values_and_stops_on_budgets():
    pol = qa.QAPolicy.for_url(URL, allowed_test_values=frozenset({"4000000000000069"}),
                              max_total_steps=5, max_seconds=10)
    assert pol.check_action(qa.Step("fill", intent="card number", value="4000000000000069"), URL, 0, 0).verdict == "allow"
    assert pol.check_action(qa.Step("assert_no_error"), URL, 5, 0).verdict == "stop"
    assert pol.check_action(qa.Step("assert_no_error"), URL, 0, 11).verdict == "stop"


# ── Agent: plan first, then run ───────────────────────────────────────────────

class FakeExecutor:
    """Records every action into the store's event log; fails steps on cue."""

    def __init__(self, store, fail_when=lambda step: False):
        self.store, self.fail_when, self.url, self.ran = store, fail_when, "about:blank", []

    async def current_url(self):
        return self.url

    async def run_step(self, step, case_id, index):
        self.store.events.append(("action", case_id, index, step.action))
        self.ran.append((case_id, step.action, step.intent or step.value))
        if step.action == "navigate":
            self.url = step.value
        if self.fail_when(step):
            return qa.StepOutcome("failed", "not as expected", self.url,
                                  f"qa/attempt_001/{case_id}_step{index:02d}.png",
                                  {"x": 1, "y": 2, "width": 3, "height": 4})
        return qa.StepOutcome("passed", "", self.url)


def run_record(instruction="", messages=(), attempts=None, **kw):
    return {"run_id": "run-1", "url": URL, "instruction": instruction,
            "environment": {"url": URL, "viewport": {"width": 1280, "height": 800}},
            "messages": list(messages), "attempts": attempts or [{"n": 1, "adjustments": {}}],
            "limits": {"max_cases": 5, "max_steps": 40}, **kw}


def go(agent, executor, attempt=1, run_id="run-1"):
    return asyncio.run(agent.run_attempt(run_id, attempt, executor, STATES))


def first(events, kind):
    return next(i for i, e in enumerate(events) if e[0] == kind)


def test_unspecified_request_persists_a_plan_before_any_action():
    store = qa.InMemoryQARunStore({"run-1": run_record("QA this site")})
    out = go(qa.QAAgent(store, signoff_module=sc), FakeExecutor(store))
    assert out["plan"]["mode"] == "planned"
    assert 1 <= len(out["plan"]["cases"]) <= 5
    actions = [e for e in store.events if e[0] == "action"]
    assert actions, "the plan ran"
    assert first(store.events, "plan_saved") < first(store.events, "action")
    assert store.attempts[("run-1", 1)]["status"] == "COMPLETED"


def test_nothing_runs_when_the_plan_cannot_be_persisted():
    class BrokenStore(qa.InMemoryQARunStore):
        def save_plan(self, run_id, attempt, plan):
            self.events.append(("plan_save_failed",))   # silently drops it

    store = BrokenStore({"run-1": run_record("QA this site")})
    with pytest.raises(qa.PlanNotPersisted):
        go(qa.QAAgent(store, signoff_module=sc), FakeExecutor(store))
    assert not any(e[0] == "action" for e in store.events)


def test_specific_instruction_runs_directly_as_the_plan():
    class Spy(qa.HeuristicTestCaseGenerator):
        called = False

        def generate(self, c, m):
            Spy.called = True
            return super().generate(c, m)

    store = qa.InMemoryQARunStore({"run-1": run_record('click Checkout then verify "Your cart"')})
    ex = FakeExecutor(store)
    out = go(qa.QAAgent(store, generator=Spy(), signoff_module=sc), ex)
    assert out["plan"]["mode"] == "specific" and not Spy.called
    assert len(out["plan"]["cases"]) == 1
    assert out["plan"]["cases"][0]["source"] == "user-instruction"
    assert ex.ran == [("TC-1", "navigate", URL), ("TC-1", "click", "Checkout"),
                      ("TC-1", "assert_text", "Your cart")]
    assert first(store.events, "plan_saved") < first(store.events, "action")


def test_specific_but_unparsable_waits_for_input_and_does_nothing():
    store = qa.InMemoryQARunStore({"run-1": run_record("test checkout with an expired card")})
    out = go(qa.QAAgent(store, signoff_module=sc), FakeExecutor(store))
    assert out["results"] is None
    assert store.attempts[("run-1", 1)]["status"] == "NEEDS_INPUT"
    assert store.attempts[("run-1", 1)]["plan"]["cases"][0]["status"] == "needs-clarification"
    assert not any(e[0] == "action" for e in store.events)


def test_policy_blocks_a_forbidden_step_without_executing_it():
    store = qa.InMemoryQARunStore({"run-1": run_record("click Place order")})
    out = go(qa.QAAgent(store, signoff_module=sc), FakeExecutor(store))
    case = out["results"]["cases"][0]
    assert case["status"] == "blocked" and "forbidden" in case["reason"]
    assert [e[3] for e in store.events if e[0] == "action"] == ["navigate"]


def test_failures_feed_the_signoff_chain():
    store = qa.InMemoryQARunStore({"run-1": run_record('click Checkout then verify "Order placed"')})
    ex = FakeExecutor(store, fail_when=lambda s: s.action == "assert_text")
    out = go(qa.QAAgent(store, signoff_module=sc), ex)
    so = out["signoff"]
    f = so["findings"][0]
    assert f["severity"] == "critical" and f["category"] == "functional"   # P0 failure
    assert f["region"] == {"x": 1, "y": 2, "width": 3, "height": 4}
    assert f["repro_steps"][1] == "Click Checkout"
    assert f["evidence"][0]["ref"].endswith("TC-1_step03.png")
    assert so["release_state"] == "pending"
    assert so["human_queue"] == [f["id"]], "no L2 configured: straight to a human"
    assert so["signoffs"]["L1"]["signer_id"] == "execution-l1"


def test_step_budget_stops_the_run():
    rec = run_record("QA this site")
    rec["limits"] = {"max_cases": 5, "max_steps": 4}
    store = qa.InMemoryQARunStore({"run-1": rec})
    out = go(qa.QAAgent(store, signoff_module=sc), FakeExecutor(store))
    assert out["results"]["summary"]["steps_executed"] <= 4
    assert sum(len(c["steps"]) for c in out["plan"]["cases"]) <= 4


# ── Sessions: continue and rerun from stored context ──────────────────────────

def two_attempts(store, messages=(), adjustments=None, instruction="QA this site"):
    store.runs["run-1"] = run_record(instruction, messages,
                                     [{"n": 1, "adjustments": {}}, {"n": 2, "adjustments": adjustments or {}}])


def test_rerun_without_new_input_redoes_the_same_plan():
    store = qa.InMemoryQARunStore({"run-1": run_record("QA this site")})
    agent = qa.QAAgent(store, signoff_module=sc)
    a1 = go(agent, FakeExecutor(store))
    two_attempts(store)
    a2 = go(agent, FakeExecutor(store), attempt=2)
    assert a2["plan"]["mode"] == "rerun"
    strip = lambda p: [(c["id"], c["title"], c["steps"]) for c in p["cases"]]
    assert strip(a2["plan"]) == strip(a1["plan"])


def test_rerun_with_case_ids_redoes_only_those():
    store = qa.InMemoryQARunStore({"run-1": run_record("QA this site")})
    agent = qa.QAAgent(store, signoff_module=sc)
    go(agent, FakeExecutor(store))
    two_attempts(store, adjustments={"case_ids": ["TC-2"]})
    a2 = go(agent, FakeExecutor(store), attempt=2)
    assert [c["id"] for c in a2["plan"]["cases"]] == ["TC-2"]


def test_follow_up_specific_instruction_replans_to_it():
    store = qa.InMemoryQARunStore({"run-1": run_record("QA this site")})
    agent = qa.QAAgent(store, signoff_module=sc)
    go(agent, FakeExecutor(store))
    later = "9999-01-01T00:00:00.000Z"
    two_attempts(store, messages=[{"kind": "followup", "text": "click Newsletter", "at": later}])
    ex = FakeExecutor(store)
    a2 = go(agent, ex, attempt=2)
    assert a2["plan"]["mode"] == "specific" and a2["plan"]["request"] == "click Newsletter"
    assert ("TC-1", "click", "Newsletter") in ex.ran


def test_correction_excludes_cases_and_adjustment_changes_viewport():
    store = qa.InMemoryQARunStore({"run-1": run_record("QA this site")})
    agent = qa.QAAgent(store, signoff_module=sc)
    go(agent, FakeExecutor(store))
    later = "9999-01-01T00:00:00.000Z"
    two_attempts(store, messages=[{"kind": "correction", "text": "no, skip the newsletter", "at": later}],
                 adjustments={"viewport": {"width": 390, "height": 844}})
    ctx2 = agent.build_context("run-1", 2, STATES)
    assert ctx2.viewport == {"width": 390, "height": 844}
    assert ctx2.exclusions == ("newsletter",)
    a2 = go(agent, FakeExecutor(store), attempt=2)
    assert a2["plan"]["mode"] == "planned"
    assert not any("newsletter" in c["title"].lower() for c in a2["plan"]["cases"])
    assert any("excluded" in n for n in a2["plan"]["notes"])


def test_rerun_adjustment_instruction_wins():
    store = qa.InMemoryQARunStore({"run-1": run_record("QA this site")})
    agent = qa.QAAgent(store, signoff_module=sc)
    go(agent, FakeExecutor(store))
    two_attempts(store, adjustments={"instruction": "click Yearly"})
    a2 = go(agent, FakeExecutor(store), attempt=2)
    assert a2["plan"]["mode"] == "specific" and a2["plan"]["request"] == "click Yearly"


def test_prior_failures_lead_the_replan():
    store = qa.InMemoryQARunStore({"run-1": run_record("QA this site")})
    agent = qa.QAAgent(store, signoff_module=sc)
    go(agent, FakeExecutor(store, fail_when=lambda s: s.intent == "Checkout"))
    two_attempts(store, adjustments={"replan": True})
    a2 = go(agent, FakeExecutor(store), attempt=2)
    assert a2["plan"]["cases"][0]["source"] == "prior-session"


# ── AWS store against stub clients ────────────────────────────────────────────

class StubDynamo:
    def __init__(self):
        self.items = {}

    def get_item(self, TableName, Key):
        item = self.items.get(Key["session_id"]["S"])
        return {"Item": item} if item else {}

    def put_item(self, TableName, Item):
        self.items[Item["session_id"]["S"]] = Item


class StubS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kw):
        assert kw.get("ServerSideEncryption") == "AES256"
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objects[Key])}


def test_aws_store_layout_and_round_trip():
    from boto3.dynamodb.types import TypeSerializer
    dyn, s3 = StubDynamo(), StubS3()
    ser = TypeSerializer()
    run = {"session_id": "QARUN#run-1", **run_record("QA this site")}
    dyn.items["QARUN#run-1"] = {k: ser.serialize(v) for k, v in run.items()}
    store = qa.AwsQARunStore("tbl", "bkt", "screenweave", dynamo=dyn, s3=s3)
    assert store.load_run("run-1")["limits"] == {"max_cases": 5, "max_steps": 40}
    agent = qa.QAAgent(store, signoff_module=sc)
    out = asyncio.run(agent.run_attempt("run-1", 1, FakeExecutor(type("E", (), {"events": []})()), STATES))
    assert set(s3.objects) == {"screenweave/run-1/qa/attempt_001/plan.json",
                               "screenweave/run-1/qa/attempt_001/results.json",
                               "screenweave/run-1/qa/attempt_001/signoff.json"}
    rec = store.load_attempt("run-1", 1)
    assert rec["status"] == "COMPLETED" and rec["plan"] == out["plan"]
    assert rec["plan_key"].endswith("plan.json") and rec["release_state"] == "pending"
    with pytest.raises(KeyError):
        store.load_run("nope")


def test_entry_point_is_inert_without_a_run_id():
    assert not qa.agent_enabled({})
    assert asyncio.run(qa.run_attempt_from_env(None, [], env={})) is None
