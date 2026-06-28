"""M2 — the stateless gates (RFC §7, design §6). Acceptance C1, C3, C4, C6, C7.

Each gate is exercised in isolation through ``gate_ctx``. A dependency failure
(missing field, hook timeout) must resolve to **fail-closed FAIL**, never an
exception and never a silent pass (CLAUDE.md, design §10/§12).
"""

from __future__ import annotations

from datetime import datetime, timezone

from acp_core.enums import Outcome
from acp_core.gating import RequestEnv
from acp_core.policy import FailureMode
from acp_gates.content import ContentHookRegistry, HookTimeout
from acp_gates.gates import (
    _WEEKDAYS,
    allowlist,
    content_check,
    denylist,
    disclosure,
    disclosure_post_check,
    emission_control,
    precondition,
    require_explanation,
    value_limit,
    window_gate,
)
from tests.conftest import gate_ctx


# --- C1 valueLimit -------------------------------------------------------
def test_c1_value_limit() -> None:
    cfg = {"field": "data.amount", "max": 10000}
    over = value_limit(cfg, gate_ctx("Payment", "pay", data={"amount": 10001}))
    assert over.outcome is Outcome.FAIL
    at = value_limit(cfg, gate_ctx("Payment", "pay", data={"amount": 10000}))
    assert at.outcome is Outcome.PASS


def test_value_limit_min_and_missing_field() -> None:
    cfg = {"field": "data.kph", "max": 130, "min": 0}
    assert value_limit(cfg, gate_ctx("Vehicle", "applySpeed", data={"kph": -5})).outcome is Outcome.FAIL
    assert value_limit(cfg, gate_ctx("Vehicle", "applySpeed", data={"kph": 50})).outcome is Outcome.PASS
    # missing field ⇒ fail-closed
    assert value_limit(cfg, gate_ctx("Vehicle", "applySpeed", data={})).outcome is Outcome.FAIL


# --- C3 allowlist / denylist --------------------------------------------
def test_c3_allowlist() -> None:
    cfg = {"field": "data.recipientDomain", "set": "corporate-domains"}
    out = allowlist(cfg, gate_ctx("Email", "sendEmail", data={"recipientDomain": "evil.example"}))
    assert out.outcome is Outcome.FAIL
    ok = allowlist(cfg, gate_ctx("Email", "sendEmail", data={"recipientDomain": "acme.example"}))
    assert ok.outcome is Outcome.PASS


def test_denylist_sanctioned() -> None:
    cfg = {"field": "data.destinationCountry", "set": "sanctioned-list"}
    assert denylist(cfg, gate_ctx("Payment", "pay", data={"destinationCountry": "KP"})).outcome is Outcome.FAIL
    assert denylist(cfg, gate_ctx("Payment", "pay", data={"destinationCountry": "US"})).outcome is Outcome.PASS


# --- C4 precondition / transition from-states ----------------------------
def test_c4_transition_from_states() -> None:
    cfg = {"from": ["conflict_check"]}
    active = gate_ctx("Matter", "engage", env=RequestEnv(resource={"currentState": "active"}))
    assert precondition(cfg, active).outcome is Outcome.FAIL  # state not in from-set
    ready = gate_ctx("Matter", "engage", env=RequestEnv(resource={"currentState": "conflict_check"}))
    assert precondition(cfg, ready).outcome is Outcome.PASS


def test_precondition_named_checks_via_flags() -> None:
    cfg = ["fiveRightsVerified", "notDiscontinued"]
    ok = gate_ctx("Patient", "administer", data={"fiveRightsVerified": True, "notDiscontinued": True})
    assert precondition(cfg, ok).outcome is Outcome.PASS
    missing = gate_ctx("Patient", "administer", data={"fiveRightsVerified": True})
    assert precondition(cfg, missing).outcome is Outcome.FAIL


def test_precondition_unknown_state_fails_closed() -> None:
    cfg = {"from": ["conflict_check"]}
    # no currentState supplied ⇒ fail-closed
    assert precondition(cfg, gate_ctx("Matter", "engage")).outcome is Outcome.FAIL


# --- requireExplanation (assess) ----------------------------------------
def test_require_explanation() -> None:
    with_rationale = gate_ctx("Patient", "triage", data={"explanation": "febrile, HR 120"})
    assert require_explanation(True, with_rationale).outcome is Outcome.PASS
    without = gate_ctx("Patient", "triage", data={})
    assert require_explanation(True, without).outcome is Outcome.FAIL


# --- window (temporal allow) --------------------------------------------
def test_window_gate_hours_and_days() -> None:
    now = datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc)
    today = _WEEKDAYS[now.weekday()]
    other = _WEEKDAYS[(now.weekday() + 1) % 7]
    in_hours = gate_ctx("Vehicle", "applySpeed", env=RequestEnv(now=now))
    assert window_gate({"hours": "09:00-16:00"}, in_hours).outcome is Outcome.PASS
    late = gate_ctx("Vehicle", "applySpeed", env=RequestEnv(now=now.replace(hour=17)))
    assert window_gate({"hours": "09:00-16:00"}, late).outcome is Outcome.FAIL
    assert window_gate({"days": [today]}, in_hours).outcome is Outcome.PASS
    assert window_gate({"days": [other]}, in_hours).outcome is Outcome.FAIL


def test_window_gate_no_clock_fails_closed() -> None:
    assert window_gate({"hours": "09:00-16:00"}, gate_ctx("Vehicle", "applySpeed")).outcome is Outcome.FAIL


# --- emissionControl -----------------------------------------------------
def test_emission_control_deconfliction() -> None:
    cfg = {"precondition": ["emconAuthorized", "deconflicted"]}
    ok = gate_ctx("Track", "radarSweep", data={"emconAuthorized": True, "deconflicted": True})
    assert emission_control(cfg, ok).outcome is Outcome.PASS
    bad = gate_ctx("Track", "radarSweep", data={"emconAuthorized": True})
    assert emission_control(cfg, bad).outcome is Outcome.FAIL


def test_emission_control_holds_for_authorization() -> None:
    cfg = {"precondition": [], "holdForAuthorization": True}
    pending = gate_ctx("Track", "radarSweep", data={})
    assert emission_control(cfg, pending).outcome is Outcome.HOLD
    authorized = gate_ctx("Track", "radarSweep", data={"emissionAuthorized": True})
    assert emission_control(cfg, authorized).outcome is Outcome.PASS


# --- C6 disclosure (pre-check and post-check) ----------------------------
def test_disclosure_pre_check_blocks_unpermitted_sink() -> None:
    cfg = {"allowSink": ["careTeam"]}
    blocked = gate_ctx("Patient", "readSealed", env=RequestEnv(sink="ops"))
    assert disclosure(cfg, blocked).outcome is Outcome.FAIL
    allowed = gate_ctx("Patient", "readSealed", env=RequestEnv(sink="careTeam"))
    assert disclosure(cfg, allowed).outcome is Outcome.PASS


def test_c6_disclosure_post_check_withholds_result() -> None:
    cfg = {"when": "action.resultSensitivity == restricted", "allowSink": ["careTeam"]}
    withheld = disclosure_post_check("restricted", cfg, sink="ops")
    assert withheld.outcome is Outcome.FAIL
    assert "withheld" in withheld.reason
    released = disclosure_post_check("restricted", cfg, sink="careTeam")
    assert released.outcome is Outcome.PASS


# --- C7 contentCheck fail-closed on timeout ------------------------------
def _timing_out_hooks() -> ContentHookRegistry:
    def boom(content: object) -> bool:
        raise HookTimeout("dlp timed out")

    return ContentHookRegistry({"dlp.basic": boom})


def test_c7_content_check_fails_closed_on_timeout() -> None:
    gctx = gate_ctx(
        "Email", "sendEmail", data={"body": "hi"},
        hooks=_timing_out_hooks(), failure_mode=FailureMode.CLOSED,
    )
    assert content_check("dlp.basic", gctx).outcome is Outcome.FAIL


def test_content_check_open_allows_on_timeout() -> None:
    gctx = gate_ctx(
        "Email", "sendEmail", data={"body": "hi"},
        hooks=_timing_out_hooks(), failure_mode=FailureMode.OPEN,
    )
    assert content_check("dlp.basic", gctx).outcome is Outcome.PASS


def test_content_check_blocks_sensitive_payload() -> None:
    gctx = gate_ctx("Email", "sendEmail", data={"body": "patient SSN 123-45-6789"})
    assert content_check("dlp.basic", gctx).outcome is Outcome.FAIL


def test_content_check_passes_clean_payload() -> None:
    gctx = gate_ctx("Email", "sendEmail", data={"body": "hello team, see you at standup"})
    assert content_check("dlp.basic", gctx).outcome is Outcome.PASS
