"""
ScreenWeave QA Agent — sessions, plan-first runs, sign-off
==========================================================
Runs one attempt of a QA run on the crawler instance, after the crawl.

A QA run is keyed by its run_id, which IS the crawl session_id (crawl_url's
id, SESSION#<id> in the sessions table). The MCP server owns the run record
(QARUN#<run_id>: instruction, follow-ups, corrections, environment, attempts);
this module owns each attempt's plan, results and sign-off. See
.claude/architecture.md ("QA sessions and run IDs").

Per attempt:
  1. Load the run record and earlier attempts (session context).
  2. Classify the request: specific instruction or unspecified.
     specific     -> the instruction itself is the plan (one test case).
     unspecified  -> generate test cases from what is known (URL, crawl
                     graph, page content, prior attempts, product description).
     rerun without new input -> reuse the previous plan exactly.
  3. Policy check the plan (case and step caps), then PERSIST it.
     Nothing touches the page until the stored plan reads back identical.
  4. Execute step by step, policy check before every action.
  5. Failed cases become findings; the sign-off chain (L1 -> L2 -> human)
     reviews them. Results and sign-off are persisted.

Opt-in: crawl.py calls this only when SCREENWEAVE_QA_RUN_ID is set, which the
MCP server does only when QA_AGENT_ENABLED is true.

Provider-agnostic: test-case generation, instruction translation and the
optional request classifier are protocols with a Bedrock adapter (same
invoke_model shape as the Visual QA worker) and deterministic implementations
that need no model and no network.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Protocol, Sequence
from urllib.parse import urljoin, urlparse

# ── Request classification (shared rule, see contracts/qa_request_classification.json) ──
# The word lists are duplicated in src/lambda/mcpServer/qaRun.mjs; both test
# suites assert they equal the contract file.

GENERIC_WORDS = frozenset("""
a about all an and app application around at audit basic bug bugs can check could do entire
everything explore find for full general go i issues it look me my of on our page pages please
problems product qa quick regressions review run site smoke some test testing tests the this through
to ui url us website whole would you
""".split())
TEST_VERBS = frozenset("check test try verify validate confirm ensure".split())
ACTION_VERBS = frozenset("""
add buy cancel checkout choose click download enter fill log login logout navigate open pay press
register remove search select sign signup submit tap type upload
""".split())

_URL_RE = re.compile(r"https?://\S+")
_QUOTED_RE = re.compile(r"\"[^\"]{2,}\"|“[^”]{2,}”|`[^`]{2,}`")
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


@dataclass(frozen=True)
class Classification:
    kind: str                 # "specific" | "unspecified"
    rule: str                 # empty | generic | quoted | action-verb | test-verb-object | ambiguous | model
    ambiguous: bool = False
    focus: tuple[str, ...] = ()
    confidence: float = 1.0


def classify_request(text: str | None) -> Classification:
    """Deterministic rule. Same steps, same lists, same results as qaRun.mjs."""
    if not text or not text.strip():
        return Classification("unspecified", "empty")
    low = text.lower()
    quoted = bool(_QUOTED_RE.search(low))
    stripped = _QUOTED_RE.sub(" ", _URL_RE.sub(" ", low))
    tokens = _TOKEN_RE.findall(stripped)
    leftover = [t for t in tokens if t not in GENERIC_WORDS]
    if not leftover and not quoted:
        return Classification("unspecified", "generic")
    if quoted:
        return Classification("specific", "quoted")
    if any(t in ACTION_VERBS for t in leftover):
        return Classification("specific", "action-verb")
    if any(t in TEST_VERBS for t in tokens) and leftover:
        return Classification("specific", "test-verb-object")
    focus = tuple(dict.fromkeys(t for t in leftover if t not in TEST_VERBS))
    return Classification("unspecified", "ambiguous", ambiguous=True, focus=focus,
                          confidence=0.5)


class RequestClassifier(Protocol):
    """Optional model: consulted only when the rule says ambiguous."""
    def classify(self, text: str) -> Classification: ...


def classify_with_optional_model(text: str | None, model: RequestClassifier | None = None,
                                 min_confidence: float = 0.7) -> Classification:
    rule = classify_request(text)
    if not rule.ambiguous or model is None:
        return rule
    try:
        m = model.classify(text or "")
    except Exception:
        return rule
    if m.kind in ("specific", "unspecified") and m.confidence >= min_confidence:
        return Classification(m.kind, "model", ambiguous=False, focus=rule.focus,
                              confidence=m.confidence)
    return rule


# ── Plan data model ───────────────────────────────────────────────────────────

STEP_ACTIONS = frozenset({"navigate", "click", "fill", "assert_text", "assert_visible",
                          "assert_url", "assert_no_error"})
CASE_SOURCES = frozenset({"user-instruction", "url", "crawl-graph", "page-content",
                          "prior-session", "product-description", "model"})
PRIORITIES = ("P0", "P1", "P2")


@dataclass
class Step:
    action: str
    intent: str = ""          # what the element is for (intent locator)
    selector: str = ""        # deterministic selector, tried first
    value: str = ""           # fill value / expected text / url fragment / path

    def describe(self) -> str:
        if self.action == "navigate":
            return f"Open {self.value}"
        if self.action == "click":
            return f"Click {self.intent or self.selector}"
        if self.action == "fill":
            return f"Fill {self.intent or self.selector} with {self.value!r}"
        if self.action == "assert_text":
            return f"Expect text {self.value!r}"
        if self.action == "assert_visible":
            return f"Expect {self.intent or self.selector} to be visible"
        if self.action == "assert_url":
            return f"Expect URL to contain {self.value!r}"
        return "Expect no error page"


@dataclass
class TestCase:
    id: str
    title: str
    steps: list[Step]
    expected: str
    priority: str = "P1"
    source: str = "model"
    status: str = "ready"     # ready | needs-clarification
    notes: list[str] = field(default_factory=list)

    __test__ = False          # not a pytest class


@dataclass
class Plan:
    run_id: str
    attempt: int
    mode: str                 # specific | planned | rerun
    classification: dict
    cases: list[TestCase]
    request: str = ""
    notes: list[str] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict:
        return json.loads(json.dumps(asdict(self), default=str))   # JSON-shaped

    def digest(self) -> str:
        body = json.dumps(self.to_dict(), sort_keys=True, default=str)
        return hashlib.sha256(body.encode()).hexdigest()


def plan_from_dict(d: dict) -> Plan:
    cases = [TestCase(id=c["id"], title=c["title"],
                      steps=[Step(**s) for s in c.get("steps", [])],
                      expected=c.get("expected", ""), priority=c.get("priority", "P1"),
                      source=c.get("source", "model"), status=c.get("status", "ready"),
                      notes=list(c.get("notes", [])))
             for c in d.get("cases", [])]
    return Plan(run_id=d["run_id"], attempt=int(d["attempt"]), mode=d["mode"],
                classification=dict(d.get("classification", {})), cases=cases,
                request=d.get("request", ""), notes=list(d.get("notes", [])),
                created_at=d.get("created_at", ""))


# ── Context the planner sees ──────────────────────────────────────────────────

@dataclass
class QAContext:
    run_id: str
    attempt: int
    url: str
    viewport: dict
    instruction: str
    messages: list[dict]                 # [{kind, text, at}]
    adjustments: dict
    product_description: str = ""
    states: list[dict] = field(default_factory=list)   # crawl states.json entries
    prior_plan: Plan | None = None
    prior_results: dict | None = None
    focus: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()

    def prior_failed_case_ids(self) -> list[str]:
        if not self.prior_results:
            return []
        return [c["case_id"] for c in self.prior_results.get("cases", [])
                if c.get("status") == "failed"]


_EXCLUDE_RE = re.compile(r"\b(?:skip|ignore|don't test|do not test|exclude|leave out)\s+(?:the\s+)?(.+?)(?:[.!,;]|$)")


def exclusions_from(messages: Sequence[dict]) -> tuple[str, ...]:
    """'skip the footer', "don't test login" in follow-ups or corrections."""
    out = []
    for m in messages:
        for hit in _EXCLUDE_RE.findall((m.get("text") or "").lower()):
            phrase = hit.strip()
            if phrase:
                out.append(phrase)
    return tuple(dict.fromkeys(out))


def effective_request(instruction: str, messages: Sequence[dict],
                      adjustments: dict) -> tuple[str, Classification]:
    """The latest specific instruction wins; otherwise the run is unspecified.

    Order considered (newest first): rerun adjustment instruction, then user
    messages newest to oldest, then the original instruction. Exclusion-only
    messages ("skip the footer") never become the instruction.
    """
    candidates = []
    if adjustments.get("instruction"):
        candidates.append(adjustments["instruction"])
    for m in reversed(list(messages)):
        if m.get("kind") in ("followup", "correction", "adjustment", "instruction"):
            t = m.get("text") or ""
            if not _EXCLUDE_RE.search(t.lower()):
                candidates.append(t)
    candidates.append(instruction or "")
    focus: list[str] = []
    for text in candidates:
        c = classify_request(text)
        if c.kind == "specific":
            return text, c
        focus.extend(c.focus)
    base = classify_request(instruction)
    return instruction or "", Classification(base.kind, base.rule, base.ambiguous,
                                             tuple(dict.fromkeys(focus)), base.confidence)


# ── Instruction translation (specific requests) ───────────────────────────────

class InstructionTranslator(Protocol):
    def translate(self, instruction: str, context: QAContext) -> TestCase: ...


_CLAUSE_SPLIT = re.compile(r"\s*(?:,\s*and\s+then\s+|,\s*then\s+|\s+and\s+then\s+|\s+then\s+|;\s*|\.\s+|,\s+)\s*",
                           re.IGNORECASE)
_Q = r"[\"“`]([^\"”`]+)[\"”`]"
_RULES: list[tuple[re.Pattern, Callable[[re.Match], list[Step]]]] = [
    (re.compile(r"^(?:go to|open|navigate to|visit)\s+(https?://\S+|/\S*)$", re.I),
     lambda m: [Step("navigate", value=m.group(1))]),
    (re.compile(r"^(?:fill|enter|type|put)\s+" + _Q + r"\s+(?:in|into)\s+(?:the\s+)?(.+?)(?:\s+field)?$", re.I),
     lambda m: [Step("fill", intent=m.group(2), value=m.group(1))]),
    (re.compile(r"^fill\s+(?:in\s+)?(?:the\s+)?(.+?)(?:\s+field)?\s+with\s+" + _Q + r"$", re.I),
     lambda m: [Step("fill", intent=m.group(1), value=m.group(2))]),
    (re.compile(r"^(?:click|tap|press|hit|choose|select)\s+(?:on\s+)?(?:the\s+)?" + _Q + r"(?:\s+(?:button|link|tab))?$", re.I),
     lambda m: [Step("click", intent=m.group(1), selector=f'text="{m.group(1)}"')]),
    (re.compile(r"^(?:click|tap|press|hit|choose|select)\s+(?:on\s+)?(?:the\s+)?(.+?)(?:\s+(?:button|link|tab))?$", re.I),
     lambda m: [Step("click", intent=m.group(1))]),
    (re.compile(r"^(?:verify|check|confirm|ensure|make sure|expect)(?:\s+that)?\s+(?:the\s+page\s+)?(?:shows|contains|displays|says|has)?\s*" + _Q + r"(?:\s+(?:is shown|appears|is visible|shows up))?$", re.I),
     lambda m: [Step("assert_text", value=m.group(1))]),
    (re.compile(r"^(?:you\s+)?should\s+see\s+" + _Q + r"$", re.I),
     lambda m: [Step("assert_text", value=m.group(1))]),
    (re.compile(r"^(?:verify|check|confirm|ensure|make sure)(?:\s+that)?\s+(?:the\s+)?(.+?)\s+(?:is visible|appears|is shown|exists|shows up)$", re.I),
     lambda m: [Step("assert_visible", intent=m.group(1))]),
    (re.compile(r"^(?:verify|check|confirm|ensure|make sure)(?:\s+that)?\s+(?:the\s+)?url\s+(?:contains|has|includes)\s+(\S+)$", re.I),
     lambda m: [Step("assert_url", value=m.group(1).strip("\"'`"))]),
    (re.compile(r"^(?:test|try|check)\s+(?:the\s+)?([a-z0-9 \-]+?)(?:\s+(?:flow|page|feature|button|link))?$", re.I),
     lambda m: [Step("click", intent=m.group(1)), Step("assert_no_error")]),
]


class RuleInstructionTranslator:
    """Deterministic translator for explicit, step-like instructions.

    Understands: open/go to <url|path>; click/tap/press <thing> or "<label>";
    fill "<value>" into <field> / fill <field> with "<value>";
    verify/expect "<text>"; verify <thing> is visible; verify url contains x;
    test <feature>. Clauses split on 'then', commas, semicolons, sentences.
    Anything it cannot parse is kept verbatim and the case is marked
    needs-clarification: it is never guessed at and never wandered around.
    """

    def translate(self, instruction: str, context: QAContext) -> TestCase:
        steps = [Step("navigate", value=context.url)]
        unparsed = []
        text = instruction.strip().rstrip(".!")
        # "test X with Y" / "test X when Y": the condition needs data or a model.
        cond = re.match(r"^(?:test|try|check)\s+(.+?)\s+(with|when|without|using|after)\s+(.+)$", text, re.I)
        if cond:
            unparsed.append(f"{cond.group(2)} {cond.group(3)}")
            text = f"test {cond.group(1)}"
        for clause in (c.strip() for c in _CLAUSE_SPLIT.split(text) if c and c.strip()):
            clause = re.sub(r"^(?:and|then|please)\s+", "", clause, flags=re.I)
            for pattern, build in _RULES:
                m = pattern.match(clause)
                if m:
                    steps.extend(build(m))
                    break
            else:
                unparsed.append(clause)
        notes = [f"could not turn into a step: {u!r}" for u in unparsed]
        return TestCase(id="TC-1", title=instruction.strip()[:120], steps=steps,
                        expected=_expected_from(steps), priority="P0",
                        source="user-instruction",
                        status="needs-clarification" if unparsed else "ready",
                        notes=notes)


def _expected_from(steps: Sequence[Step]) -> str:
    asserts = [s.describe() for s in steps if s.action.startswith("assert")]
    return "; ".join(asserts) if asserts else "Every step completes without an error page"


# ── Test-case generation (unspecified requests) ───────────────────────────────

class TestCaseGenerator(Protocol):
    def generate(self, context: QAContext, max_cases: int) -> list[TestCase]: ...


_FLOW_KEYWORDS = ("checkout", "cart", "login", "log in", "sign in", "sign up", "signup",
                  "register", "search", "contact", "subscribe", "pricing", "book", "download")


class HeuristicTestCaseGenerator:
    """Deterministic generator. No model, no network, zero cost.

    Sources, in priority order:
      prior-session        cases that failed in the previous attempt (P0)
      product-description  flows the description names that the crawl saw (P0)
      url / page-content   page loads with its main heading, per crawled page (P1)
      crawl-graph          each interactive element the crawl found responds (P2)
    Focus words (from an ambiguous request) move matching cases first.
    """

    __test__ = False

    def generate(self, context: QAContext, max_cases: int) -> list[TestCase]:
        cases: list[TestCase] = []
        seen_titles: set[str] = set()

        def add(tc: TestCase):
            key = tc.title.lower()
            if key not in seen_titles:
                seen_titles.add(key)
                cases.append(tc)

        if context.prior_plan:
            failed = set(context.prior_failed_case_ids())
            for c in context.prior_plan.cases:
                if c.id in failed:
                    add(TestCase(id="", title=f"Re-test: {c.title}", steps=list(c.steps),
                                 expected=c.expected, priority="P0", source="prior-session"))

        labels: dict[str, str] = {}      # label -> url where seen
        for s in context.states:
            for label in s.get("interactive_elements") or []:
                label = str(label).strip()
                if label and label.lower() not in (k.lower() for k in labels):
                    labels[label] = s.get("url") or context.url

        desc = (context.product_description or "").lower()
        for kw in _FLOW_KEYWORDS:
            if kw in desc:
                match = next((lbl for lbl in labels if kw in lbl.lower()), None)
                if match:
                    add(TestCase(id="", title=f"Primary flow: {match}",
                                 steps=[Step("navigate", value=labels[match]),
                                        Step("click", intent=match, selector=f'text="{match}"'),
                                        Step("assert_no_error")],
                                 expected=f"'{match}' opens without an error page",
                                 priority="P0", source="product-description"))

        pages: dict[str, dict] = {}
        for s in context.states:
            if s.get("trigger_action") == "navigation" and s.get("url") not in pages:
                pages[s.get("url")] = s
        if not pages:
            pages[context.url] = {"url": context.url, "headings": []}
        for url, s in pages.items():
            heading = next((h.get("text") for h in s.get("headings") or [] if h.get("text")), "")
            steps = [Step("navigate", value=url), Step("assert_no_error")]
            if heading:
                steps.append(Step("assert_text", value=heading))
            path = urlparse(url).path or "/"
            add(TestCase(id="", title=f"Page loads: {path}", steps=steps,
                         expected=f"Page renders{' with heading ' + repr(heading) if heading else ''} and no error",
                         priority="P1", source="page-content" if heading else "url"))

        for label, url in labels.items():
            add(TestCase(id="", title=f"Control responds: {label}",
                         steps=[Step("navigate", value=url),
                                Step("click", intent=label, selector=f'text="{label}"'),
                                Step("assert_no_error")],
                         expected=f"Clicking '{label}' shows no error page",
                         priority="P2", source="crawl-graph"))

        if context.focus:
            def hit(tc):
                t = tc.title.lower()
                return any(f in t for f in context.focus)
            cases.sort(key=lambda tc: (not hit(tc), PRIORITIES.index(tc.priority)))
        else:
            cases.sort(key=lambda tc: PRIORITIES.index(tc.priority))
        return cases[: max(0, max_cases)]


# ── Bedrock adapters (optional; same request shape as visualQAWorker) ─────────

DEFAULT_PLANNER_MODEL = os.environ.get(
    "SCREENWEAVE_QA_PLANNER_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
_BACKOFF = (2, 4, 8)


class BedrockJson:
    """invoke_model with an anthropic_version body; returns parsed JSON."""

    def __init__(self, model_id: str = DEFAULT_PLANNER_MODEL, client=None,
                 region: str | None = None, max_tokens: int = 2000, sleep=time.sleep):
        self.model_id, self._client, self.max_tokens, self._sleep = model_id, client, max_tokens, sleep
        self.region = region or os.environ.get("SCREENWEAVE_REGION", os.environ.get("AWS_REGION", "us-east-1"))

    @property
    def client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    def ask(self, system: str, text: str) -> Any:
        from botocore.exceptions import ClientError
        body = json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": self.max_tokens,
                           "system": system,
                           "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]})
        last = None
        for delay in [0] + list(_BACKOFF):
            if delay:
                self._sleep(delay)
            try:
                resp = self.client.invoke_model(modelId=self.model_id, contentType="application/json",
                                                accept="application/json", body=body)
                reply = json.loads(resp["body"].read())["content"][0]["text"]
                m = re.search(r"(\{.*\}|\[.*\])", reply, re.DOTALL)
                return json.loads(m.group(1)) if m else None
            except ClientError as exc:
                if exc.response["Error"]["Code"] in ("ThrottlingException", "ServiceUnavailableException"):
                    last = exc
                    continue
                raise
            except (json.JSONDecodeError, KeyError, IndexError):
                return None
        raise RuntimeError(f"Bedrock still throttling after {len(_BACKOFF)} retries") from last


def _context_summary(context: QAContext) -> dict:
    return {
        "url": context.url, "viewport": context.viewport,
        "product_description": context.product_description[:1500],
        "instruction": context.instruction, "focus": list(context.focus),
        "exclusions": list(context.exclusions),
        "user_messages": [m.get("text", "")[:300] for m in context.messages[-10:]],
        "pages": [{"url": s.get("url"), "title": s.get("title"),
                   "headings": [h.get("text") for h in (s.get("headings") or [])][:5],
                   "controls": (s.get("interactive_elements") or [])[:15]}
                  for s in context.states if s.get("trigger_action") == "navigation"][:15],
        "previous_failures": context.prior_failed_case_ids(),
    }


def cases_from_json(data: Any, default_source: str, max_cases: int, max_steps: int) -> list[TestCase]:
    """Validate model output into TestCases. Bad entries are dropped, never repaired."""
    items = data.get("cases") if isinstance(data, dict) else data
    out: list[TestCase] = []
    for raw in items or []:
        if not isinstance(raw, dict) or not raw.get("title"):
            continue
        steps = []
        for s in raw.get("steps") or []:
            if isinstance(s, dict) and s.get("action") in STEP_ACTIONS:
                steps.append(Step(action=s["action"], intent=str(s.get("intent", ""))[:120],
                                  selector=str(s.get("selector", ""))[:200],
                                  value=str(s.get("value", ""))[:300]))
        if not steps:
            continue
        src = raw.get("source") if raw.get("source") in CASE_SOURCES else default_source
        pri = raw.get("priority") if raw.get("priority") in PRIORITIES else "P1"
        out.append(TestCase(id="", title=str(raw["title"])[:120], steps=steps[:max_steps],
                            expected=str(raw.get("expected", ""))[:300], priority=pri, source=src))
        if len(out) >= max_cases:
            break
    return out


_STEP_SCHEMA = ('steps: [{"action": one of navigate|click|fill|assert_text|assert_visible|assert_url|'
                'assert_no_error, "intent": what the element is for, "selector": optional CSS, '
                '"value": url, path, fill value or expected text}]')


class BedrockTestCaseGenerator:
    __test__ = False

    def __init__(self, llm: BedrockJson | None = None, max_steps: int = 12):
        self.llm, self.max_steps = llm or BedrockJson(), max_steps

    def generate(self, context: QAContext, max_cases: int) -> list[TestCase]:
        system = ("You write manual QA test cases for a web product. Use only what the context "
                  "shows. No destructive actions, no real credentials or payment details. Reply "
                  'with JSON only: {"cases": [{"title", "priority": P0|P1|P2, "source": one of '
                  "url|crawl-graph|page-content|prior-session|product-description, "
                  f'"expected", {_STEP_SCHEMA}}}]}}. At most {max_cases} cases.')
        data = self.llm.ask(system, json.dumps(_context_summary(context)))
        return cases_from_json(data, "model", max_cases, self.max_steps)


class BedrockInstructionTranslator:
    def __init__(self, llm: BedrockJson | None = None, max_steps: int = 12):
        self.llm, self.max_steps = llm or BedrockJson(), max_steps

    def translate(self, instruction: str, context: QAContext) -> TestCase:
        system = ("Turn the user's QA instruction into ONE executable test case. Do exactly what "
                  "the instruction says and nothing more. Reply with JSON only: "
                  f'{{"title", "expected", {_STEP_SCHEMA}}}. If it cannot be done with these '
                  'actions, reply {"title": "...", "steps": [], "missing": "what is needed"}.')
        data = self.llm.ask(system, json.dumps({"instruction": instruction,
                                                **_context_summary(context)}))
        cases = cases_from_json([data] if isinstance(data, dict) else data,
                                "user-instruction", 1, self.max_steps)
        if not cases:
            missing = data.get("missing", "") if isinstance(data, dict) else ""
            return TestCase(id="TC-1", title=instruction[:120], steps=[Step("navigate", value=context.url)],
                            expected="", priority="P0", source="user-instruction",
                            status="needs-clarification",
                            notes=[f"model could not translate the instruction{': ' + missing if missing else ''}"])
        tc = cases[0]
        tc.id, tc.priority, tc.source, tc.title = "TC-1", "P0", "user-instruction", instruction[:120]
        if not tc.steps or tc.steps[0].action != "navigate":
            tc.steps.insert(0, Step("navigate", value=context.url))
        return tc


class BedrockRequestClassifier:
    def __init__(self, llm: BedrockJson | None = None):
        self.llm = llm or BedrockJson(max_tokens=200)

    def classify(self, text: str) -> Classification:
        system = ('Is this QA request a specific instruction (a concrete flow, element or condition '
                  'to test) or unspecified (general testing)? Reply JSON only: {"kind": '
                  '"specific"|"unspecified", "confidence": 0..1}')
        data = self.llm.ask(system, text) or {}
        try:
            conf = max(0.0, min(float(data.get("confidence", 0)), 1.0))
        except (TypeError, ValueError):
            conf = 0.0
        return Classification(str(data.get("kind", "")), "model", confidence=conf)


# ── Policy (what to do, what to avoid, when to stop) ──────────────────────────

_FORBIDDEN_INTENT = re.compile(
    r"\b(delete|remove account|close account|deactivate|purchase|buy now|place order|confirm order|"
    r"pay now|pay|transfer|send money|unsubscribe|cancel subscription|wipe|reset all)\b", re.I)
_SENSITIVE_FIELD = re.compile(
    r"\b(password|passcode|pin|otp|one[- ]time|verification code|2fa|mfa|card number|credit card|"
    r"debit card|cvv|cvc|security code|expiry|expiration|ssn|social security|iban|routing|account number)\b", re.I)


@dataclass
class Decision:
    verdict: str              # allow | deny | stop
    reason: str = ""


@dataclass
class QAPolicy:
    allowed_domains: frozenset[str]
    max_cases: int = 8
    max_steps_per_case: int = 12
    max_total_steps: int = 60
    max_seconds: float = 600.0
    allowed_test_values: frozenset[str] = frozenset()   # operator-approved test data for sensitive fields

    @classmethod
    def for_url(cls, url: str, **kw) -> "QAPolicy":
        return cls(allowed_domains=frozenset({urlparse(url).hostname or ""}), **kw)

    def check_plan(self, plan: Plan) -> list[str]:
        """Trim the plan to the caps in place. Returns notes describing cuts."""
        notes = []
        if len(plan.cases) > self.max_cases:
            notes.append(f"plan cut from {len(plan.cases)} to {self.max_cases} cases (max_cases)")
            plan.cases = plan.cases[: self.max_cases]
        total = 0
        kept = []
        for c in plan.cases:
            if len(c.steps) > self.max_steps_per_case:
                notes.append(f"{c.id}: steps cut from {len(c.steps)} to {self.max_steps_per_case}")
                c.steps = c.steps[: self.max_steps_per_case]
            if total + len(c.steps) > self.max_total_steps:
                notes.append(f"{c.id} and later cases dropped (max_total_steps {self.max_total_steps})")
                break
            total += len(c.steps)
            kept.append(c)
        plan.cases = kept
        return notes

    def domain_ok(self, url: str) -> bool:
        host = urlparse(url).hostname or ""
        return any(host == d or host.endswith("." + d) for d in self.allowed_domains if d)

    def check_action(self, step: Step, current_url: str, steps_done: int,
                     elapsed_s: float) -> Decision:
        if steps_done >= self.max_total_steps:
            return Decision("stop", f"step budget reached ({self.max_total_steps})")
        if elapsed_s >= self.max_seconds:
            return Decision("stop", f"time budget reached ({self.max_seconds:.0f}s)")
        if step.action not in STEP_ACTIONS:
            return Decision("deny", f"unknown action {step.action!r}")
        if step.action == "navigate":
            target = urljoin(current_url or "", step.value)
            if urlparse(target).scheme not in ("http", "https") or not self.domain_ok(target):
                return Decision("deny", f"navigation outside allowed domains: {target}")
        if step.action == "click" and _FORBIDDEN_INTENT.search(f"{step.intent} {step.selector}"):
            return Decision("deny", f"forbidden action: {step.intent or step.selector}")
        if step.action == "fill":
            target = f"{step.intent} {step.selector}"
            if _SENSITIVE_FIELD.search(target) and step.value not in self.allowed_test_values:
                return Decision("deny", f"credential or payment field: {step.intent or step.selector}")
        return Decision("allow")


# ── Execution ─────────────────────────────────────────────────────────────────

@dataclass
class StepOutcome:
    status: str               # passed | failed | error
    detail: str = ""
    url: str = ""
    screenshot_key: str = ""
    region: dict | None = None


class Executor(Protocol):
    async def current_url(self) -> str: ...
    async def run_step(self, step: Step, case_id: str, index: int) -> StepOutcome: ...


_ERROR_PAGE = re.compile(r"\b(404|500|502|503|page not found|not found|internal server error|"
                         r"something went wrong|an error occurred)\b", re.I)


class PlaywrightExecutor:
    """Runs steps on a live page: selector first, intent locator second."""

    def __init__(self, page, finder=None, intent_enabled: bool | None = None,
                 out_dir: str = "/opt/output/qa", key_prefix: str = "", screenshots: bool = True):
        self.page, self.finder, self.intent_enabled = page, finder, intent_enabled
        self.out_dir, self.key_prefix, self.screenshots = out_dir, key_prefix, screenshots
        self.last_status: int | None = None

    async def current_url(self) -> str:
        return self.page.url

    async def _shot(self, case_id, index) -> str:
        if not self.screenshots:
            return ""
        os.makedirs(self.out_dir, exist_ok=True)
        name = f"{case_id}_step{index:02d}.png"
        await self.page.screenshot(path=os.path.join(self.out_dir, name), full_page=True)
        return f"{self.key_prefix}{name}"

    async def _resolve(self, step):
        import intent_locator as il
        return await il.resolve_stable(self.page, step.selector or None, step.intent or None,
                                       self.finder, enabled=self.intent_enabled)

    async def run_step(self, step: Step, case_id: str, index: int) -> StepOutcome:
        p = self.page
        try:
            if step.action == "navigate":
                resp = await p.goto(urljoin(p.url if p.url.startswith("http") else "", step.value),
                                    wait_until="domcontentloaded", timeout=25000)
                self.last_status = resp.status if resp else None
                if resp and resp.status >= 400:
                    return StepOutcome("failed", f"HTTP {resp.status}", p.url, await self._shot(case_id, index))
                return StepOutcome("passed", "", p.url)
            if step.action in ("click", "fill", "assert_visible"):
                res = await self._resolve(step)
                if not res.found:
                    return StepOutcome("failed", f"element not found: {res.reason}", p.url,
                                       await self._shot(case_id, index))
                box = await res.locator.bounding_box()
                region = ({"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]}
                          if box else None)
                if step.action == "click":
                    await res.locator.click(timeout=5000)
                    try:
                        await p.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        pass
                elif step.action == "fill":
                    await res.locator.fill(step.value, timeout=5000)
                return StepOutcome("passed", res.strategy, p.url, region=region)
            if step.action == "assert_text":
                body = await p.evaluate("document.body ? document.body.innerText : ''")
                ok = step.value.lower() in body.lower()
                return StepOutcome("passed" if ok else "failed",
                                   "" if ok else f"text not found: {step.value!r}", p.url,
                                   "" if ok else await self._shot(case_id, index))
            if step.action == "assert_url":
                ok = step.value in p.url
                return StepOutcome("passed" if ok else "failed",
                                   "" if ok else f"url {p.url} lacks {step.value!r}", p.url)
            if step.action == "assert_no_error":
                text = f"{await p.title()} {(await p.evaluate('document.body ? document.body.innerText.slice(0, 2000) : \"\"'))}"
                hit = _ERROR_PAGE.search(text)
                bad_status = self.last_status is not None and self.last_status >= 400
                if hit or bad_status:
                    return StepOutcome("failed", f"error page: {hit.group(0) if hit else self.last_status}",
                                       p.url, await self._shot(case_id, index))
                return StepOutcome("passed", "", p.url)
            return StepOutcome("error", f"unsupported action {step.action}", p.url)
        except Exception as exc:
            return StepOutcome("error", str(exc)[:300], p.url)


# ── Session store ─────────────────────────────────────────────────────────────

class QARunStore(Protocol):
    def load_run(self, run_id: str) -> dict: ...
    def load_attempt(self, run_id: str, attempt: int) -> dict | None: ...
    def save_plan(self, run_id: str, attempt: int, plan: dict) -> None: ...
    def save_status(self, run_id: str, attempt: int, status: str, **extra) -> None: ...
    def save_results(self, run_id: str, attempt: int, results: dict, signoff: dict | None) -> None: ...


def _now() -> str:
    """Same shape as JavaScript's toISOString(), so timestamps from the MCP
    server and from here compare correctly as strings."""
    t = datetime.now(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


class InMemoryQARunStore:
    """Local fake. `events` is an ordered log tests use to check sequencing."""

    def __init__(self, runs: dict | None = None):
        self.runs = runs or {}
        self.attempts: dict[tuple[str, int], dict] = {}
        self.events: list[tuple] = []

    def load_run(self, run_id):
        if run_id not in self.runs:
            raise KeyError(f"QA run not found: {run_id}")
        return json.loads(json.dumps(self.runs[run_id]))

    def load_attempt(self, run_id, attempt):
        a = self.attempts.get((run_id, attempt))
        return json.loads(json.dumps(a)) if a else None

    def save_plan(self, run_id, attempt, plan):
        rec = self.attempts.setdefault((run_id, attempt), {})
        rec.update(plan=json.loads(json.dumps(plan)), status="PLANNED")
        self.events.append(("plan_saved", run_id, attempt))

    def save_status(self, run_id, attempt, status, **extra):
        rec = self.attempts.setdefault((run_id, attempt), {})
        rec.update(status=status, **extra)
        self.events.append(("status", run_id, attempt, status))

    def save_results(self, run_id, attempt, results, signoff):
        rec = self.attempts.setdefault((run_id, attempt), {})
        rec.update(results=json.loads(json.dumps(results)),
                   signoff=json.loads(json.dumps(signoff)) if signoff else None)
        self.events.append(("results_saved", run_id, attempt))


def _plain(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


class AwsQARunStore:
    """The persisted design: the sessions table plus the artifacts bucket.

    DynamoDB (sessions table, existing):
      QARUN#<run_id>             written by the MCP server: instruction,
                                 messages, environment, attempts, limits.
      QARUN#<run_id>#ATTEMPT#<n> written here: status, plan digest, counts.
    S3 (artifacts bucket, existing):
      <prefix>/<run_id>/qa/attempt_<nnn>/plan.json | results.json | signoff.json
    """

    def __init__(self, table: str, bucket: str, prefix: str = "screenweave",
                 dynamo=None, s3=None, region: str | None = None, ttl_days: int = 30):
        self.table, self.bucket, self.prefix, self.ttl_days = table, bucket, prefix, ttl_days
        self.region = region or os.environ.get("SCREENWEAVE_REGION", "us-east-1")
        self._dynamo, self._s3 = dynamo, s3

    @property
    def dynamo(self):
        if self._dynamo is None:
            import boto3
            self._dynamo = boto3.client("dynamodb", region_name=self.region)
        return self._dynamo

    @property
    def s3(self):
        if self._s3 is None:
            import boto3
            self._s3 = boto3.client("s3", region_name=self.region)
        return self._s3

    def key(self, run_id, attempt, name):
        return f"{self.prefix}/{run_id}/qa/attempt_{attempt:03d}/{name}"

    def _get(self, pk):
        from boto3.dynamodb.types import TypeDeserializer
        item = self.dynamo.get_item(TableName=self.table, Key={"session_id": {"S": pk}}).get("Item")
        if not item:
            return None
        d = TypeDeserializer()
        return _plain({k: d.deserialize(v) for k, v in item.items()})

    def _put_attempt(self, run_id, attempt, fields):
        from boto3.dynamodb.types import TypeSerializer
        ser = TypeSerializer()
        current = self._get(f"QARUN#{run_id}#ATTEMPT#{attempt}") or {}
        current.update(fields)
        current.update(session_id=f"QARUN#{run_id}#ATTEMPT#{attempt}", run_id=run_id,
                       attempt=attempt, updated_at=_now(),
                       ttl=int(time.time()) + self.ttl_days * 86400)
        clean = json.loads(json.dumps(current, default=str))
        self.dynamo.put_item(TableName=self.table,
                             Item={k: ser.serialize(v) for k, v in clean.items() if v is not None})

    def _put_json(self, key, data):
        self.s3.put_object(Bucket=self.bucket, Key=key, ContentType="application/json",
                           ServerSideEncryption="AES256",
                           Body=json.dumps(data, indent=2, default=str).encode())

    def _get_json(self, key):
        try:
            return json.loads(self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read())
        except Exception:
            return None

    def load_run(self, run_id):
        run = self._get(f"QARUN#{run_id}")
        if not run:
            raise KeyError(f"QA run not found: {run_id}")
        return run

    def load_attempt(self, run_id, attempt):
        rec = self._get(f"QARUN#{run_id}#ATTEMPT#{attempt}")
        if not rec:
            return None
        rec["plan"] = self._get_json(self.key(run_id, attempt, "plan.json"))
        rec["results"] = self._get_json(self.key(run_id, attempt, "results.json"))
        return rec

    def save_plan(self, run_id, attempt, plan):
        key = self.key(run_id, attempt, "plan.json")
        self._put_json(key, plan)
        self._put_attempt(run_id, attempt, {"status": "PLANNED", "plan_key": key,
                                            "cases": len(plan.get("cases", [])),
                                            "mode": plan.get("mode")})

    def save_status(self, run_id, attempt, status, **extra):
        self._put_attempt(run_id, attempt, {"status": status, **extra})

    def save_results(self, run_id, attempt, results, signoff):
        rkey = self.key(run_id, attempt, "results.json")
        self._put_json(rkey, results)
        fields = {"results_key": rkey, "summary": results.get("summary")}
        if signoff:
            skey = self.key(run_id, attempt, "signoff.json")
            self._put_json(skey, signoff)
            fields.update(signoff_key=skey, release_state=signoff.get("release_state"))
        self._put_attempt(run_id, attempt, fields)


# ── Sign-off chain glue ───────────────────────────────────────────────────────

_PRIORITY_SEVERITY = {"P0": "critical", "P1": "major", "P2": "minor"}


class ExecutionL1Reviewer:
    """L1 for executed test cases: a failed case is a finding. Deterministic, free."""
    reviewer_id = "execution-l1"
    cost_per_call_usd = 0.0

    def __init__(self, sc):
        self.sc = sc

    def review(self, subject, batch):
        sc = self.sc
        out = []
        for case in batch:
            if case.get("status") != "failed":
                continue
            failed = next((s for s in case["steps"] if s["status"] in ("failed", "error")), None) or {}
            evidence = [sc.Evidence("screenshot", s["screenshot_key"], s.get("detail", ""))
                        for s in case["steps"] if s.get("screenshot_key")]
            region = failed.get("region")
            out.append(sc.Finding(
                title=f"{case['case_id']} failed: {case['title']}"[:160],
                severity=sc.Severity(_PRIORITY_SEVERITY.get(case.get("priority"), "major")),
                category="functional", state_id=case["case_id"], url=failed.get("url", ""),
                description=f"Expected: {case.get('expected', '')}. Got: {failed.get('detail', '')}",
                screenshot_key=failed.get("screenshot_key", ""),
                region=sc.Region(**region) if region else None,
                repro_steps=[s["description"] for s in case["steps"]],
                evidence=evidence, confidence=0.9, raised_by=self.reviewer_id))
        return out


class NoL2Reviewer:
    """Placeholder until an L2 model reviewer is configured: its budget is zero,
    so the chain sends every escalation straight to the human queue."""
    reviewer_id = "l2-not-configured"
    cost_per_call_usd = 0.0

    def review(self, findings):
        raise RuntimeError("no L2 reviewer configured")


def run_signoff(run_id: str, release: str, case_results: list[dict],
                l2=None, sc=None) -> dict | None:
    if sc is None:
        try:
            import signoff_chain as sc
        except ImportError:
            return None
    chain = sc.SignOffChain(ExecutionL1Reviewer(sc), l2 or NoL2Reviewer(),
                            l2_budget=sc.TierBudget(max_calls=None if l2 else 0))
    run = chain.run(sc.ReviewSubject(session_id=run_id, states=case_results, release=release))
    return run.to_dict()


# ── The agent ─────────────────────────────────────────────────────────────────

class PlanNotPersisted(RuntimeError):
    pass


class QAAgent:
    def __init__(self, store: QARunStore,
                 generator: TestCaseGenerator | None = None,
                 translator: InstructionTranslator | None = None,
                 classifier: RequestClassifier | None = None,
                 l2_reviewer=None, signoff_module=None,
                 clock: Callable[[], float] = time.monotonic, log: Callable[..., None] | None = None):
        self.store = store
        self.generator = generator or HeuristicTestCaseGenerator()
        self.translator = translator or RuleInstructionTranslator()
        self.classifier = classifier
        self.l2_reviewer = l2_reviewer
        self.signoff_module = signoff_module
        self.clock = clock
        self.log = log or (lambda level, msg, **kw: None)

    # Context ──────────────────────────────────────────────────────────────────
    def build_context(self, run_id: str, attempt: int, states: Sequence[dict]) -> QAContext:
        run = self.store.load_run(run_id)
        attempts = run.get("attempts") or []
        adj = next((a.get("adjustments") or {} for a in attempts if int(a.get("n", 0)) == attempt), {})
        prior_plan = prior_results = None
        for n in range(attempt - 1, 0, -1):
            rec = self.store.load_attempt(run_id, n)
            if rec and rec.get("plan"):
                prior_plan = plan_from_dict(rec["plan"])
                prior_results = rec.get("results")
                break
        env = dict(run.get("environment") or {})
        if adj.get("viewport"):
            env["viewport"] = adj["viewport"]
        if adj.get("url"):
            env["url"] = adj["url"]
        messages = list(run.get("messages") or [])
        return QAContext(run_id=run_id, attempt=attempt, url=env.get("url") or run.get("url", ""),
                         viewport=env.get("viewport") or {"width": 1280, "height": 800},
                         instruction=run.get("instruction") or "", messages=messages,
                         adjustments=adj, product_description=run.get("product_description") or "",
                         states=list(states), prior_plan=prior_plan, prior_results=prior_results,
                         exclusions=exclusions_from(messages))

    def _new_input_since_prior(self, ctx: QAContext) -> bool:
        if ctx.adjustments.get("instruction") or ctx.adjustments.get("replan"):
            return True
        if not ctx.prior_plan:
            return True
        if ctx.adjustments.get("case_ids"):
            return False          # "redo these cases": always the stored plan
        cutoff = ctx.prior_plan.created_at
        return any((m.get("at") or "") > cutoff for m in ctx.messages if m.get("kind") != "instruction")

    # Planning ────────────────────────────────────────────────────────────────
    def make_plan(self, ctx: QAContext, policy: QAPolicy) -> Plan:
        if not self._new_input_since_prior(ctx):
            plan = plan_from_dict(ctx.prior_plan.to_dict())
            plan.attempt, plan.mode, plan.notes = ctx.attempt, "rerun", [
                f"re-running the plan from attempt {ctx.prior_plan.attempt} unchanged"]
            plan.created_at = _now()
        else:
            text, cls = effective_request(ctx.instruction, ctx.messages, ctx.adjustments)
            if cls.ambiguous and self.classifier is not None:
                cls = classify_with_optional_model(text, self.classifier)
            ctx.focus = cls.focus
            if cls.kind == "specific":
                case = self.translator.translate(text, ctx)
                plan = Plan(ctx.run_id, ctx.attempt, "specific", asdict(cls), [case], request=text)
            else:
                cases = self.generator.generate(ctx, policy.max_cases * 2)
                if ctx.exclusions:
                    cases = [c for c in cases
                             if not any(x in c.title.lower() for x in ctx.exclusions)]
                plan = Plan(ctx.run_id, ctx.attempt, "planned", asdict(cls), cases, request=text)
                if ctx.exclusions:
                    plan.notes.append("excluded: " + ", ".join(ctx.exclusions))
            plan.created_at = _now()
        ids = ctx.adjustments.get("case_ids")
        if ids and plan.mode == "rerun":
            plan.cases = [c for c in plan.cases if c.id in set(ids)]
            plan.notes.append("limited to cases " + ", ".join(ids))
        for i, c in enumerate(plan.cases, 1):
            if not c.id or plan.mode == "planned":
                c.id = f"TC-{i}"
        plan.notes.extend(policy.check_plan(plan))
        return plan

    def persist_plan(self, plan: Plan) -> None:
        self.store.save_plan(plan.run_id, plan.attempt, plan.to_dict())
        back = self.store.load_attempt(plan.run_id, plan.attempt) or {}
        if not back.get("plan") or plan_from_dict(back["plan"]).digest() != plan.digest():
            raise PlanNotPersisted("stored plan does not match; refusing to execute")

    # Execution ──────────────────────────────────────────────────────────────
    async def execute(self, plan: Plan, executor: Executor, policy: QAPolicy) -> dict:
        start = self.clock()
        steps_done = 0
        stopped = ""
        cases_out = []
        for case in plan.cases:
            rec = {"case_id": case.id, "state_id": case.id, "title": case.title,
                   "priority": case.priority, "source": case.source,
                   "expected": case.expected, "steps": [], "status": "passed"}
            if stopped:
                rec["status"] = "not-run"
                rec["reason"] = stopped
                cases_out.append(rec)
                continue
            if case.status != "ready":
                rec["status"] = "needs-clarification"
                rec["reason"] = "; ".join(case.notes)
                cases_out.append(rec)
                continue
            for i, step in enumerate(case.steps, 1):
                d = policy.check_action(step, await executor.current_url(), steps_done,
                                        self.clock() - start)
                srec = {"index": i, "action": step.action, "description": step.describe(),
                        "policy": d.verdict, "policy_reason": d.reason}
                if d.verdict == "stop":
                    stopped = d.reason
                    srec["status"] = "not-run"
                    rec["steps"].append(srec)
                    rec["status"] = "not-run"
                    rec["reason"] = d.reason
                    break
                if d.verdict == "deny":
                    srec["status"] = "blocked"
                    rec["steps"].append(srec)
                    rec["status"] = "blocked"
                    rec["reason"] = d.reason
                    break
                out = await executor.run_step(step, case.id, i)
                steps_done += 1
                srec.update(status=out.status, detail=out.detail, url=out.url,
                            screenshot_key=out.screenshot_key, region=out.region)
                rec["steps"].append(srec)
                if out.status != "passed":
                    rec["status"] = "failed"
                    break
            cases_out.append(rec)
        summary = {s: sum(1 for c in cases_out if c["status"] == s)
                   for s in ("passed", "failed", "blocked", "not-run", "needs-clarification")}
        summary["steps_executed"] = steps_done
        return {"run_id": plan.run_id, "attempt": plan.attempt, "mode": plan.mode,
                "plan_digest": plan.digest(), "cases": cases_out, "summary": summary,
                "stopped": stopped, "finished_at": _now()}

    # One attempt ────────────────────────────────────────────────────────────
    async def run_attempt(self, run_id: str, attempt: int, executor: Executor,
                          states: Sequence[dict] = (), policy: QAPolicy | None = None) -> dict:
        ctx = self.build_context(run_id, attempt, states)
        run = self.store.load_run(run_id)
        limits = run.get("limits") or {}
        adj_limits = {k: ctx.adjustments[k] for k in ("max_cases", "max_steps") if ctx.adjustments.get(k)}
        limits = {**limits, **adj_limits}
        policy = policy or QAPolicy.for_url(
            ctx.url, max_cases=int(limits.get("max_cases", 8)),
            max_total_steps=int(limits.get("max_steps", 60)))
        self.store.save_status(run_id, attempt, "PLANNING")
        plan = self.make_plan(ctx, policy)
        self.persist_plan(plan)                         # plan first, always
        self.log("INFO", "QA plan persisted", run_id=run_id, attempt=attempt,
                 mode=plan.mode, cases=len(plan.cases))
        if not plan.cases or all(c.status != "ready" for c in plan.cases):
            self.store.save_status(run_id, attempt, "NEEDS_INPUT",
                                   reason="no executable test case; reply with continue_qa_run")
            return {"plan": plan.to_dict(), "results": None, "signoff": None}
        self.store.save_status(run_id, attempt, "RUNNING")
        results = await self.execute(plan, executor, policy)
        signoff = run_signoff(run_id, f"attempt {attempt}", results["cases"],
                              l2=self.l2_reviewer, sc=self.signoff_module)
        self.store.save_results(run_id, attempt, results, signoff)
        self.store.save_status(run_id, attempt, "COMPLETED")
        return {"plan": plan.to_dict(), "results": results, "signoff": signoff}


# ── Entry point used by crawl.py ──────────────────────────────────────────────

def agent_enabled(env=None) -> bool:
    env = env if env is not None else os.environ
    return bool(env.get("SCREENWEAVE_QA_RUN_ID"))


async def run_attempt_from_env(page, states: Sequence[dict], log=None, env=None) -> dict | None:
    """Called by crawl.py after the crawl when SCREENWEAVE_QA_RUN_ID is set."""
    env = env if env is not None else os.environ
    if not agent_enabled(env):
        return None
    run_id = env["SCREENWEAVE_QA_RUN_ID"]
    attempt = int(env.get("SCREENWEAVE_QA_ATTEMPT", "1"))
    store = AwsQARunStore(table=env.get("SCREENWEAVE_DYNAMO_TABLE", ""),
                          bucket=env.get("SCREENWEAVE_S3_BUCKET", ""),
                          prefix=env.get("SCREENWEAVE_S3_PREFIX", "screenweave"))
    use_model = env.get("SCREENWEAVE_QA_PLANNER", "heuristic").lower() == "bedrock"
    finder = None
    try:
        import intent_locator as il
        if il.intent_locator_enabled(env):
            finder = il.IntentFinder(il.BedrockVisionModel(), max_model_calls=20)
    except ImportError:
        il = None
    agent = QAAgent(store,
                    generator=BedrockTestCaseGenerator() if use_model else None,
                    translator=BedrockInstructionTranslator() if use_model else None,
                    classifier=BedrockRequestClassifier() if use_model else None,
                    log=log)
    out_dir = f"/opt/output/qa/attempt_{attempt:03d}"
    executor = PlaywrightExecutor(page, finder=finder, intent_enabled=finder is not None,
                                  out_dir=out_dir,
                                  key_prefix=f"qa/attempt_{attempt:03d}/")
    try:
        return await agent.run_attempt(run_id, attempt, executor, states)
    except Exception as exc:
        store.save_status(run_id, attempt, "FAILED", reason=str(exc)[:300])
        raise
