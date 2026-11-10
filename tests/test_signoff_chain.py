"""QA sign-off chain: L1 → L2 → human, with fake reviewers. No model calls."""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "lambda" / "visualQAWorker"))

import signoff_chain as sc  # noqa: E402

S = sc.Severity


def states(n):
    return [{"state_id": f"state_{i:04d}", "url": f"https://app.example/p{i}"} for i in range(1, n + 1)]


def finding(fid, sev, conf=0.9, category="visual", state="state_0001"):
    return sc.Finding(id=fid, title=f"{fid} title", severity=sev, category=category,
                      state_id=state, url="https://app.example/p1",
                      screenshot_key=f"screenshots/{state}.png",
                      region=sc.Region(10, 20, 300, 40),
                      repro_steps=["open /p1", "scroll to footer"],
                      evidence=[sc.Evidence("screenshot", f"screenshots/{state}.png")],
                      confidence=conf)


class FakeL1:
    reviewer_id = "fake-l1"
    cost_per_call_usd = 0.002   # example number only

    def __init__(self, by_state=None, fail_on=None):
        self.by_state = by_state or {}
        self.fail_on = fail_on
        self.batches = []

    def review(self, subject, batch):
        ids = [s["state_id"] for s in batch]
        self.batches.append(ids)
        if self.fail_on and self.fail_on in ids:
            raise RuntimeError("model timeout")
        out = []
        for sid in ids:
            out.extend(self.by_state.get(sid, []))
        return out


class FakeL2:
    reviewer_id = "fake-l2"
    cost_per_call_usd = 0.03    # example number only

    def __init__(self, verdicts=None, fail=False):
        self.verdicts = verdicts or {}
        self.fail = fail
        self.seen = []

    def review(self, findings):
        self.seen.append([f.id for f in findings])
        if self.fail:
            raise RuntimeError("model unavailable")
        return [self.verdicts[f.id] for f in findings if f.id in self.verdicts]


def V(fid, decision, conf=0.9, new=None, human=False):
    return sc.L2Verdict(fid, sc.L2Decision(decision), "because", conf, new, human)


def clock():
    clock.t += 1
    return f"2026-11-10T00:00:{clock.t:02d}+00:00"


clock.t = 0

SUBJECT = sc.ReviewSubject("sess-1", states(25), release="v1.2.3")


def chain(l1, l2, **kw):
    return sc.SignOffChain(l1, l2, clock=clock, **kw)


# ── Happy paths ───────────────────────────────────────────────────────────────

def test_clean_run_stops_at_l1_and_never_calls_l2():
    l1, l2 = FakeL1(), FakeL2()
    run = chain(l1, l2).run(SUBJECT)
    assert l1.batches and len(l1.batches) == 3          # 25 states / 10 per call
    assert l2.seen == []
    assert run.signoffs[sc.Level.L1].state is sc.SignOffState.APPROVED
    l2s = run.signoffs[sc.Level.L2]
    assert l2s.state is sc.SignOffState.APPROVED and l2s.signer_kind is sc.SignerKind.RULE
    assert run.release_state is sc.SignOffState.PENDING, "only a human approves a release"
    assert run.cost == {"L1": {"calls": 3, "usd": 0.006}, "L2": {"calls": 0, "usd": 0.0}}


def test_minor_confident_findings_stay_at_l1():
    l1 = FakeL1({"state_0002": [finding("f1", S.COSMETIC), finding("f2", S.MINOR)]})
    l2 = FakeL2()
    run = chain(l1, l2).run(SUBJECT)
    assert [f.status for f in run.findings] == [sc.FindingStatus.OPEN] * 2
    assert l2.seen == [] and run.human_queue() == []


@pytest.mark.parametrize("f,reason", [
    (finding("f1", S.MAJOR), "severity major"),
    (finding("f1", S.MINOR, conf=0.5), "L1 confidence"),
    (finding("f1", S.COSMETIC, category="accessibility"), "category accessibility"),
])
def test_l1_escalation_rules(f, reason):
    run = chain(FakeL1({"state_0001": [f]}), FakeL2({"f1": V("f1", "dismiss")})).run(SUBJECT)
    assert any(reason in r for r in run.findings[0].escalation_reasons)
    assert run.signoffs[sc.Level.L1].state is sc.SignOffState.NEEDS_CHANGES


def test_l2_sees_only_escalated_findings():
    l1 = FakeL1({"state_0001": [finding("keep", S.MAJOR), finding("noise", S.COSMETIC)]})
    l2 = FakeL2({"keep": V("keep", "confirm")})
    chain(l1, l2).run(SUBJECT)
    assert l2.seen == [["keep"]]


def test_l2_dismisses_false_positive_and_approves():
    l1 = FakeL1({"state_0003": [finding("f1", S.MAJOR)]})
    run = chain(l1, FakeL2({"f1": V("f1", "dismiss")})).run(SUBJECT)
    assert run.findings[0].status is sc.FindingStatus.DISMISSED
    assert run.signoffs[sc.Level.L2].state is sc.SignOffState.APPROVED
    assert run.human_queue() == []


def test_l2_reclassifies_and_keeps_original_severity():
    l1 = FakeL1({"state_0003": [finding("f1", S.MAJOR)]})
    run = chain(l1, FakeL2({"f1": V("f1", "reclassify", new=S.MINOR)})).run(SUBJECT)
    f = run.findings[0]
    assert f.status is sc.FindingStatus.RECLASSIFIED
    assert f.severity is S.MINOR and f.original_severity is S.MAJOR
    assert run.signoffs[sc.Level.L2].state is sc.SignOffState.APPROVED


def test_confirmed_major_needs_changes():
    l1 = FakeL1({"state_0003": [finding("f1", S.MAJOR)]})
    run = chain(l1, FakeL2({"f1": V("f1", "confirm")})).run(SUBJECT)
    assert run.signoffs[sc.Level.L2].state is sc.SignOffState.NEEDS_CHANGES
    assert run.human_queue() == []


def test_confirmed_critical_rejects_and_goes_to_human():
    l1 = FakeL1({"state_0003": [finding("f1", S.CRITICAL)]})
    run = chain(l1, FakeL2({"f1": V("f1", "confirm")})).run(SUBJECT)
    assert run.signoffs[sc.Level.L2].state is sc.SignOffState.REJECTED
    assert [f.id for f in run.human_queue()] == ["f1"]


# ── L2 → human rules ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("verdict,reason", [
    (V("f1", "confirm", human=True), "L2 asked for a human"),
    (V("f1", "confirm", conf=0.4), "L2 confidence"),
])
def test_l2_to_human_rules(verdict, reason):
    run = chain(FakeL1({"state_0001": [finding("f1", S.MAJOR)]}), FakeL2({"f1": verdict})).run(SUBJECT)
    assert run.findings[0].needs_human
    assert any(reason in r for r in run.findings[0].escalation_reasons)


@pytest.mark.parametrize("verdict", [V("f1", "dismiss"), V("f1", "reclassify", new=S.MINOR)])
def test_dismissing_or_downgrading_a_critical_still_reaches_a_human(verdict):
    run = chain(FakeL1({"state_0001": [finding("f1", S.CRITICAL)]}), FakeL2({"f1": verdict})).run(SUBJECT)
    assert run.findings[0].needs_human
    assert any("dismissed or downgraded a critical" in r for r in run.findings[0].escalation_reasons)


def test_missing_unknown_and_duplicate_verdicts():
    l1 = FakeL1({"state_0001": [finding("a", S.MAJOR), finding("b", S.MAJOR)]})

    class L2:
        reviewer_id, cost_per_call_usd = "odd-l2", 0.03

        def review(self, fs):
            return [V("a", "dismiss"), V("a", "confirm"), V("ghost", "confirm")]

    run = chain(l1, L2()).run(SUBJECT)
    a, b = run.findings
    assert a.status is sc.FindingStatus.DISMISSED, "first verdict wins, duplicate ignored"
    assert b.needs_human and "L2 returned no verdict" in b.escalation_reasons
    ignored = [e for e in run.audit if e.action == "ignored"]
    assert {e.detail["finding_id"] for e in ignored} == {"a", "ghost"}
    assert run.signoffs[sc.Level.L2].state is sc.SignOffState.NEEDS_CHANGES


# ── Budgets and failures fail toward a human, never toward approval ───────────

def test_l1_budget_exhaustion_leaves_states_unreviewed_not_approved():
    run = chain(FakeL1(), FakeL2(), l1_budget=sc.TierBudget(max_calls=2)).run(SUBJECT)
    assert len(run.unreviewed_state_ids) == 5
    assert run.signoffs[sc.Level.L1].state is sc.SignOffState.NEEDS_CHANGES
    with pytest.raises(ValueError, match="never reviewed"):
        run.record_human_signoff("raja", sc.SignOffState.APPROVED)


def test_l1_cost_cap():
    run = chain(FakeL1(), FakeL2(), l1_budget=sc.TierBudget(max_cost_usd=0.004)).run(SUBJECT)
    assert run.cost["L1"] == {"calls": 2, "usd": 0.004}
    assert len(run.unreviewed_state_ids) == 5


def test_l1_reviewer_error_is_recorded_and_not_a_pass():
    run = chain(FakeL1(fail_on="state_0011"), FakeL2()).run(SUBJECT)
    assert run.unreviewed_state_ids[0] == "state_0011" and len(run.unreviewed_state_ids) == 10
    assert any(e.action == "error" and e.level is sc.Level.L1 for e in run.audit)
    assert run.signoffs[sc.Level.L1].state is sc.SignOffState.NEEDS_CHANGES


def test_l2_budget_exhaustion_sends_findings_to_a_human():
    fs = [finding(f"f{i}", S.MAJOR) for i in range(3)]
    l2 = FakeL2({f.id: V(f.id, "dismiss") for f in fs})
    run = chain(FakeL1({"state_0001": fs}), l2,
                rules=sc.EscalationRules(findings_per_l2_call=2),
                l2_budget=sc.TierBudget(max_calls=1)).run(SUBJECT)
    assert l2.seen == [["f0", "f1"]]
    assert [f.id for f in run.human_queue()] == ["f2"]
    assert run.signoffs[sc.Level.L2].state is sc.SignOffState.NEEDS_CHANGES


def test_l2_error_sends_everything_to_a_human():
    run = chain(FakeL1({"state_0001": [finding("f1", S.MAJOR)]}), FakeL2(fail=True)).run(SUBJECT)
    assert run.findings[0].needs_human
    assert run.signoffs[sc.Level.L2].state is sc.SignOffState.NEEDS_CHANGES


# ── L3: the human gate ────────────────────────────────────────────────────────

def test_only_a_human_can_sign_l3():
    run = chain(FakeL1(), FakeL2()).run(SUBJECT)
    with pytest.raises(PermissionError):
        run._sign(sc.Level.L3, sc.SignOffState.APPROVED, sc.SignerKind.MODEL, "fake-l2", "lgtm")
    with pytest.raises(PermissionError):
        run._sign(sc.Level.L3, sc.SignOffState.APPROVED, sc.SignerKind.RULE, "auto", "lgtm")
    assert run.release_state is sc.SignOffState.PENDING


@pytest.mark.parametrize("state", [sc.SignOffState.APPROVED, sc.SignOffState.REJECTED,
                                   sc.SignOffState.NEEDS_CHANGES])
def test_human_decides_the_release(state):
    run = chain(FakeL1(), FakeL2()).run(SUBJECT)
    s = run.record_human_signoff("raja", state, "checked the queue")
    assert s.signer_kind is sc.SignerKind.HUMAN and s.signer_id == "raja"
    assert run.release_state is state


def test_human_signoff_validation():
    run = chain(FakeL1({"state_0001": [finding("f1", S.CRITICAL)]}),
                FakeL2({"f1": V("f1", "confirm")})).run(SUBJECT)
    with pytest.raises(ValueError):
        run.record_human_signoff("", sc.SignOffState.APPROVED, "x")
    with pytest.raises(ValueError):
        run.record_human_signoff("raja", sc.SignOffState.PENDING)
    with pytest.raises(ValueError, match="critical"):
        run.record_human_signoff("raja", sc.SignOffState.APPROVED)
    run.record_human_signoff("raja", sc.SignOffState.APPROVED, "known issue, hotfix tracked")
    assert run.release_state is sc.SignOffState.APPROVED


# ── Audit trail and serialisation ─────────────────────────────────────────────

def test_audit_trail_names_who_signed_each_level():
    run = chain(FakeL1({"state_0001": [finding("f1", S.MAJOR)]}),
                FakeL2({"f1": V("f1", "confirm")})).run(SUBJECT)
    run.record_human_signoff("raja", sc.SignOffState.NEEDS_CHANGES, "fix f1 first")
    assert list(sc.iter_signers(run)) == [
        ("L1", "needs-changes", "model", "fake-l1"),
        ("L2", "needs-changes", "model", "fake-l2"),
        ("L3", "needs-changes", "human", "raja"),
    ]
    signoff_events = [e.action for e in run.audit if e.action.startswith("signoff:")]
    assert signoff_events == ["signoff:needs-changes", "signoff:needs-changes",
                              "signoff:pending", "signoff:needs-changes"]
    times = [e.at for e in run.audit]
    assert times == sorted(times), "append-only, in order"


def test_to_dict_is_json_serialisable():
    run = chain(FakeL1({"state_0001": [finding("f1", S.CRITICAL)]}),
                FakeL2({"f1": V("f1", "reclassify", new=S.MAJOR)})).run(SUBJECT)
    d = json.loads(json.dumps(run.to_dict()))
    assert d["release_state"] == "pending"
    assert d["findings"][0]["severity"] == "major"
    assert d["findings"][0]["original_severity"] == "critical"
    assert d["findings"][0]["region"] == {"x": 10, "y": 20, "width": 300, "height": 40}
    assert d["signoffs"]["L3"]["signer_kind"] == "rule"
    assert d["human_queue"] == ["f1"]


def test_estimate_run_cost_example():
    # Illustrative inputs only (see .claude/architecture.md cost model).
    est = sc.estimate_run_cost(states=200, l1_cost_per_call=0.004, l2_cost_per_call=0.05,
                               escalation_rate=0.2)
    assert est["l1_calls"] == 20 and est["l2_calls"] == 1
    assert est["escalated_findings"] == 12.0
    assert est["total_usd"] == pytest.approx(0.13)
