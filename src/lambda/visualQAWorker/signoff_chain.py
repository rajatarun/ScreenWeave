"""
ScreenWeave — QA Sign-off Chain
===============================
Mirrors how a QA organisation signs off a release, at a fraction of the cost:

  L1  cheap AI first-pass reviewer (small model). Sees every captured state,
      records issues as structured Findings: severity, screenshot region,
      repro steps, evidence.
  L2  AI senior-QA reviewer (stronger model). Sees ONLY what L1 escalated.
      Confirms, dismisses or reclassifies each finding and decides which ones
      a human must look at.
  L3  Human final sign-off. The only level allowed to approve a release.

Each level runs less often and costs more than the one below, so most work
stops at L1. See .claude/architecture.md ("QA sign-off chain").

Provider-agnostic: reviewers are small protocols (L1Reviewer, L2Reviewer).
This module makes no model calls and imports nothing outside the standard
library. It is not used by handler.py; it is an opt-in building block.

Safety rules enforced here, not left to reviewers:
  - Nothing but a human signer can approve L3, and only L3 approves a release.
  - Any budget exhaustion or reviewer error escalates; it never approves.
  - Every decision is appended to an audit trail with who/what made it.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Iterable, Protocol, Sequence


# ── Vocabulary ────────────────────────────────────────────────────────────────

class Severity(str, Enum):
    CRITICAL = "critical"   # blocks release: broken core flow, data loss, crash
    MAJOR    = "major"      # feature wrong or unusable, no workaround
    MINOR    = "minor"      # wrong but has a workaround; visible defect
    COSMETIC = "cosmetic"   # polish: spacing, alignment, colour

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {Severity.COSMETIC: 0, Severity.MINOR: 1,
                  Severity.MAJOR: 2, Severity.CRITICAL: 3}


class SignOffState(str, Enum):
    PENDING       = "pending"
    APPROVED      = "approved"
    REJECTED      = "rejected"
    NEEDS_CHANGES = "needs-changes"


class Level(str, Enum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class SignerKind(str, Enum):
    MODEL = "model"   # an AI reviewer; id is the reviewer/model id
    HUMAN = "human"   # a person; id is their name or handle
    RULE  = "rule"    # a deterministic chain rule (e.g. "nothing escalated")


class FindingStatus(str, Enum):
    OPEN         = "open"          # raised by L1, not escalated (L1-only record)
    ESCALATED    = "escalated"     # waiting for L2
    CONFIRMED    = "confirmed"     # L2 agrees
    DISMISSED    = "dismissed"     # L2 says false positive
    RECLASSIFIED = "reclassified"  # L2 agrees it's real, different severity


class L2Decision(str, Enum):
    CONFIRM    = "confirm"
    DISMISS    = "dismiss"
    RECLASSIFY = "reclassify"


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Region:
    """Screenshot region in CSS pixels of the full-page screenshot."""
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class Evidence:
    kind: str        # "screenshot" | "dom" | "console" | "network" | "trace" | "diff"
    ref: str         # S3 key, state_id, selector, log line...
    note: str = ""


@dataclass
class Finding:
    title: str
    severity: Severity
    category: str                     # visual | layout | functional | content | accessibility
    state_id: str
    url: str
    description: str = ""
    screenshot_key: str = ""          # e.g. screenshots/state_0007.png
    region: Region | None = None
    repro_steps: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    confidence: float = 0.0           # reviewer's confidence this is a real defect
    raised_by: str = ""               # L1 reviewer id
    id: str = field(default_factory=lambda: f"f_{uuid.uuid4().hex[:10]}")
    status: FindingStatus = FindingStatus.OPEN
    original_severity: Severity | None = None
    needs_human: bool = False
    escalation_reasons: list[str] = field(default_factory=list)
    l2_rationale: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        d["status"] = self.status.value
        d["original_severity"] = self.original_severity.value if self.original_severity else None
        return d


@dataclass(frozen=True)
class L2Verdict:
    finding_id: str
    decision: L2Decision
    rationale: str
    confidence: float
    new_severity: Severity | None = None   # required for RECLASSIFY
    needs_human: bool = False


@dataclass(frozen=True)
class ReviewSubject:
    """What L1 looks at: one crawl/explore session's captured states."""
    session_id: str
    states: Sequence[dict]               # states.json entries
    release: str = ""                    # build / version label under review


@dataclass(frozen=True)
class SignOff:
    level: Level
    state: SignOffState
    signer_kind: SignerKind
    signer_id: str
    at: str
    rationale: str = ""
    finding_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuditEvent:
    at: str
    level: Level
    actor_kind: SignerKind
    actor_id: str
    action: str
    detail: dict


# ── Reviewer protocols ────────────────────────────────────────────────────────

class L1Reviewer(Protocol):
    reviewer_id: str
    cost_per_call_usd: float           # estimate used for budget accounting

    def review(self, subject: ReviewSubject, states: Sequence[dict]) -> list[Finding]: ...


class L2Reviewer(Protocol):
    reviewer_id: str
    cost_per_call_usd: float

    def review(self, findings: Sequence[Finding]) -> list[L2Verdict]: ...


# ── Rules and budgets ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EscalationRules:
    """When L1 hands a finding to L2, and when L2 hands it to a human.

    Defaults are starting points, not tuned values.
    """
    # L1 → L2
    escalate_min_severity: Severity = Severity.MAJOR   # this severity or worse
    escalate_below_confidence: float = 0.70            # L1 unsure → senior look
    always_escalate_categories: frozenset[str] = frozenset({"accessibility", "functional"})
    # L2 → human
    human_min_severity: Severity = Severity.CRITICAL   # confirmed this bad → human
    human_below_confidence: float = 0.60               # L2 unsure → human
    human_on_downgrade_from: Severity = Severity.CRITICAL  # L2 downgraded/dismissed a critical
    # Batching
    states_per_l1_call: int = 10                       # matches the worker's batch size
    findings_per_l2_call: int = 20


@dataclass
class TierBudget:
    max_calls: int | None = None
    max_cost_usd: float | None = None
    calls: int = 0
    cost_usd: float = 0.0

    def allows(self, next_cost: float) -> bool:
        if self.max_calls is not None and self.calls + 1 > self.max_calls:
            return False
        if self.max_cost_usd is not None and self.cost_usd + next_cost > self.max_cost_usd + 1e-12:
            return False
        return True

    def charge(self, cost: float) -> None:
        self.calls += 1
        self.cost_usd = round(self.cost_usd + cost, 6)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── The chain ─────────────────────────────────────────────────────────────────

class SignOffChain:
    """Runs L1 then L2 on one ReviewSubject and leaves L3 pending for a human.

        chain = SignOffChain(l1, l2, rules, l1_budget, l2_budget)
        run = chain.run(subject)         # L1 + L2; L3 is PENDING
        run.record_human_signoff("raja", SignOffState.APPROVED, "looks good")
    """

    def __init__(self, l1: L1Reviewer, l2: L2Reviewer,
                 rules: EscalationRules | None = None,
                 l1_budget: TierBudget | None = None,
                 l2_budget: TierBudget | None = None,
                 clock: Callable[[], str] = _now):
        self.l1, self.l2 = l1, l2
        self.rules = rules or EscalationRules()
        self.l1_budget = l1_budget or TierBudget()
        self.l2_budget = l2_budget or TierBudget()
        self.clock = clock

    # L1 ───────────────────────────────────────────────────────────────────────
    def _l1_escalation_reasons(self, f: Finding) -> list[str]:
        r = self.rules
        reasons = []
        if f.severity.rank >= r.escalate_min_severity.rank:
            reasons.append(f"severity {f.severity.value} >= {r.escalate_min_severity.value}")
        if f.confidence < r.escalate_below_confidence:
            reasons.append(f"L1 confidence {f.confidence:.2f} < {r.escalate_below_confidence:.2f}")
        if f.category in r.always_escalate_categories:
            reasons.append(f"category {f.category} always escalates")
        return reasons

    def _run_l1(self, run: "ReviewRun", subject: ReviewSubject) -> None:
        states = list(subject.states)
        size = max(1, self.rules.states_per_l1_call)
        unreviewed: list[dict] = []
        for i in range(0, len(states), size):
            batch = states[i:i + size]
            if not self.l1_budget.allows(self.l1.cost_per_call_usd):
                unreviewed.extend(batch)
                continue
            self.l1_budget.charge(self.l1.cost_per_call_usd)
            try:
                found = self.l1.review(subject, batch)
            except Exception as exc:  # reviewer failure must not look like a clean pass
                run._audit(Level.L1, SignerKind.MODEL, self.l1.reviewer_id, "error",
                           {"error": str(exc)[:300], "state_ids": [s.get("state_id") for s in batch]})
                unreviewed.extend(batch)
                continue
            for f in found:
                f.raised_by = f.raised_by or self.l1.reviewer_id
                reasons = self._l1_escalation_reasons(f)
                if reasons:
                    f.status = FindingStatus.ESCALATED
                    f.escalation_reasons = reasons
                run.findings.append(f)
                run._audit(Level.L1, SignerKind.MODEL, self.l1.reviewer_id, "finding",
                           {"finding_id": f.id, "severity": f.severity.value,
                            "status": f.status.value, "reasons": reasons})

        run.unreviewed_state_ids = [s.get("state_id", "") for s in unreviewed]
        escalated = run.findings_with(FindingStatus.ESCALATED)
        if unreviewed:
            state, why = SignOffState.NEEDS_CHANGES, (
                f"{len(unreviewed)} state(s) not reviewed (budget or reviewer error)")
        elif escalated:
            state, why = SignOffState.NEEDS_CHANGES, f"{len(escalated)} finding(s) escalated to L2"
        else:
            state, why = SignOffState.APPROVED, "no finding met an escalation rule"
        run._sign(Level.L1, state, SignerKind.MODEL, self.l1.reviewer_id, why,
                  [f.id for f in run.findings])

    # L2 ───────────────────────────────────────────────────────────────────────
    def _apply_verdict(self, run: "ReviewRun", f: Finding, v: L2Verdict) -> None:
        r = self.rules
        original = f.severity
        if v.decision is L2Decision.DISMISS:
            f.status = FindingStatus.DISMISSED
        elif v.decision is L2Decision.RECLASSIFY and v.new_severity and v.new_severity != f.severity:
            f.status = FindingStatus.RECLASSIFIED
            f.original_severity = f.severity
            f.severity = v.new_severity
        else:
            f.status = FindingStatus.CONFIRMED
        f.l2_rationale = v.rationale
        human_reasons = []
        if v.needs_human:
            human_reasons.append("L2 asked for a human")
        if f.status is not FindingStatus.DISMISSED and f.severity.rank >= r.human_min_severity.rank:
            human_reasons.append(f"confirmed {f.severity.value}")
        if v.confidence < r.human_below_confidence:
            human_reasons.append(f"L2 confidence {v.confidence:.2f} < {r.human_below_confidence:.2f}")
        if original.rank >= r.human_on_downgrade_from.rank and (
                f.status is FindingStatus.DISMISSED or f.severity.rank < original.rank):
            human_reasons.append(f"L2 dismissed or downgraded a {original.value}")
        if human_reasons:
            f.needs_human = True
            f.escalation_reasons = f.escalation_reasons + human_reasons
        run._audit(Level.L2, SignerKind.MODEL, self.l2.reviewer_id, v.decision.value,
                   {"finding_id": f.id, "status": f.status.value, "severity": f.severity.value,
                    "needs_human": f.needs_human, "confidence": v.confidence,
                    "reasons": human_reasons})

    def _run_l2(self, run: "ReviewRun") -> None:
        pending = run.findings_with(FindingStatus.ESCALATED)
        if not pending:
            run._sign(Level.L2, SignOffState.APPROVED, SignerKind.RULE, "nothing-escalated",
                      "L1 escalated nothing; L2 not called")
            return
        size = max(1, self.rules.findings_per_l2_call)
        for i in range(0, len(pending), size):
            batch = pending[i:i + size]
            if not self.l2_budget.allows(self.l2.cost_per_call_usd):
                for f in batch:
                    f.needs_human = True
                    f.escalation_reasons.append("L2 budget exhausted; sent straight to a human")
                run._audit(Level.L2, SignerKind.RULE, "budget", "skipped",
                           {"finding_ids": [f.id for f in batch]})
                continue
            self.l2_budget.charge(self.l2.cost_per_call_usd)
            try:
                verdicts = self.l2.review(batch)
            except Exception as exc:
                for f in batch:
                    f.needs_human = True
                    f.escalation_reasons.append("L2 reviewer error; sent to a human")
                run._audit(Level.L2, SignerKind.MODEL, self.l2.reviewer_id, "error",
                           {"error": str(exc)[:300]})
                continue
            by_id = {f.id: f for f in batch}
            seen = set()
            for v in verdicts:
                f = by_id.get(v.finding_id)
                if f is None or v.finding_id in seen:   # unknown or duplicate id
                    run._audit(Level.L2, SignerKind.MODEL, self.l2.reviewer_id, "ignored",
                               {"finding_id": v.finding_id, "why": "unknown or duplicate"})
                    continue
                seen.add(v.finding_id)
                self._apply_verdict(run, f, v)
            for f in batch:
                if f.id not in seen:                     # L2 skipped it: a human decides
                    f.needs_human = True
                    f.escalation_reasons.append("L2 returned no verdict")

        live = [f for f in run.findings
                if f.status in (FindingStatus.CONFIRMED, FindingStatus.RECLASSIFIED)]
        unresolved = [f for f in pending if f.status is FindingStatus.ESCALATED]
        if any(f.severity is Severity.CRITICAL for f in live):
            state, why = SignOffState.REJECTED, "confirmed critical finding"
        elif unresolved or any(f.severity.rank >= Severity.MAJOR.rank for f in live):
            state, why = SignOffState.NEEDS_CHANGES, (
                f"{len(unresolved)} unresolved, "
                f"{sum(f.severity.rank >= Severity.MAJOR.rank for f in live)} confirmed major+")
        else:
            state, why = SignOffState.APPROVED, "escalations dismissed or minor"
        run._sign(Level.L2, state, SignerKind.MODEL, self.l2.reviewer_id, why,
                  [f.id for f in pending])

    # Entry point ──────────────────────────────────────────────────────────────
    def run(self, subject: ReviewSubject) -> "ReviewRun":
        run = ReviewRun(subject=subject, clock=self.clock)
        self._run_l1(run, subject)
        self._run_l2(run)
        run._sign(Level.L3, SignOffState.PENDING, SignerKind.RULE, "awaiting-human",
                  f"{len(run.human_queue())} finding(s) flagged for the human reviewer")
        run.cost = {"L1": {"calls": self.l1_budget.calls, "usd": self.l1_budget.cost_usd},
                    "L2": {"calls": self.l2_budget.calls, "usd": self.l2_budget.cost_usd}}
        return run


@dataclass
class ReviewRun:
    subject: ReviewSubject
    clock: Callable[[], str] = _now
    findings: list[Finding] = field(default_factory=list)
    signoffs: dict = field(default_factory=dict)        # Level -> SignOff (latest)
    audit: list[AuditEvent] = field(default_factory=list)
    unreviewed_state_ids: list[str] = field(default_factory=list)
    cost: dict = field(default_factory=dict)

    def _audit(self, level, kind, actor, action, detail) -> None:
        self.audit.append(AuditEvent(self.clock(), level, kind, actor, action, detail))

    def _sign(self, level, state, kind, signer, rationale, finding_ids=()) -> SignOff:
        if level is Level.L3 and state is not SignOffState.PENDING and kind is not SignerKind.HUMAN:
            raise PermissionError("only a human can sign off L3")
        s = SignOff(level, state, kind, signer, self.clock(), rationale, tuple(finding_ids))
        self.signoffs[level] = s
        self._audit(level, kind, signer, f"signoff:{state.value}",
                    {"rationale": rationale, "finding_ids": list(finding_ids)})
        return s

    def findings_with(self, status: FindingStatus) -> list[Finding]:
        return [f for f in self.findings if f.status is status]

    def human_queue(self) -> list[Finding]:
        """What the L3 reviewer must look at: flagged findings, worst first."""
        q = [f for f in self.findings if f.needs_human]
        return sorted(q, key=lambda f: -f.severity.rank)

    def record_human_signoff(self, reviewer: str, state: SignOffState,
                             note: str = "") -> SignOff:
        """The L3 gate. Approving over a confirmed critical needs a written note."""
        if not reviewer or not reviewer.strip():
            raise ValueError("human sign-off needs a reviewer name")
        if state is SignOffState.PENDING:
            raise ValueError("a sign-off decision cannot be 'pending'")
        if self.unreviewed_state_ids and state is SignOffState.APPROVED and not note.strip():
            raise ValueError("some states were never reviewed; approving needs a note")
        open_critical = [f for f in self.findings
                         if f.severity is Severity.CRITICAL
                         and f.status in (FindingStatus.CONFIRMED, FindingStatus.RECLASSIFIED)]
        if open_critical and state is SignOffState.APPROVED and not note.strip():
            raise ValueError("approving over a confirmed critical finding needs a note")
        return self._sign(Level.L3, state, SignerKind.HUMAN, reviewer.strip(), note,
                          [f.id for f in self.human_queue()])

    @property
    def release_state(self) -> SignOffState:
        """Only L3 decides. Until a human signs, the release is pending."""
        l3 = self.signoffs.get(Level.L3)
        return l3.state if l3 else SignOffState.PENDING

    def to_dict(self) -> dict:
        def so(s: SignOff) -> dict:
            d = asdict(s)
            d.update(level=s.level.value, state=s.state.value, signer_kind=s.signer_kind.value,
                     finding_ids=list(s.finding_ids))
            return d
        return {
            "session_id": self.subject.session_id,
            "release": self.subject.release,
            "release_state": self.release_state.value,
            "signoffs": {lvl.value: so(s) for lvl, s in self.signoffs.items()},
            "findings": [f.to_dict() for f in self.findings],
            "human_queue": [f.id for f in self.human_queue()],
            "unreviewed_state_ids": self.unreviewed_state_ids,
            "cost": self.cost,
            "audit": [{**asdict(e), "level": e.level.value, "actor_kind": e.actor_kind.value}
                      for e in self.audit],
        }


def estimate_run_cost(states: int, l1_cost_per_call: float, l2_cost_per_call: float,
                      escalation_rate: float, states_per_l1_call: int = 10,
                      findings_per_state: float = 0.3, findings_per_l2_call: int = 20) -> dict:
    """Back-of-envelope cost for one run, for the cost model in the docs.

    Every input is an assumption the caller supplies; nothing here is a price.
    """
    import math
    l1_calls = math.ceil(states / max(1, states_per_l1_call)) if states else 0
    escalated = states * findings_per_state * escalation_rate
    l2_calls = math.ceil(escalated / max(1, findings_per_l2_call)) if escalated else 0
    return {"l1_calls": l1_calls, "l2_calls": l2_calls,
            "escalated_findings": round(escalated, 2),
            "l1_usd": round(l1_calls * l1_cost_per_call, 4),
            "l2_usd": round(l2_calls * l2_cost_per_call, 4),
            "total_usd": round(l1_calls * l1_cost_per_call + l2_calls * l2_cost_per_call, 4)}


def iter_signers(run: ReviewRun) -> Iterable[tuple[str, str, str, str]]:
    """(level, state, signer_kind, signer_id) per level, for reports."""
    for lvl in (Level.L1, Level.L2, Level.L3):
        s = run.signoffs.get(lvl)
        if s:
            yield (lvl.value, s.state.value, s.signer_kind.value, s.signer_id)
