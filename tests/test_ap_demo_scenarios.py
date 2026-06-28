"""End-to-end demo scenarios, driven by the fake-LLM agent (no key, no Docker).

These run the *real* agent tool-loop and the *real* gateway over the unmodified
``payments-ops.acp.yaml``; only the LLM is the scripted fake. The same scenarios
run against Claude/OpenAI by swapping the provider — see ``acp_ap_demo.__main__``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from acp_ap_demo.fake_llm import FakeProvider
from acp_ap_demo.gateway import APBundle, build_inmemory_bundle
from acp_ap_demo.llm import LLMProvider
from acp_ap_demo.scenarios import (
    approve_and_settle,
    scenario_approval,
    scenario_blocked,
    scenario_happy,
    scenario_process_inbox,
)

DEMO_NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def bundle() -> APBundle:
    return build_inmemory_bundle(clock=lambda: DEMO_NOW)


@pytest.fixture
def provider() -> LLMProvider:
    return FakeProvider()


def test_happy_pays_the_small_invoice(bundle: APBundle, provider: LLMProvider) -> None:
    result = scenario_happy(bundle, provider)
    assert len(result.payments) == 1
    assert result.payments[0]["amount"] == 800.0
    assert any(d["decision"] == "allow" for d in result.decisions)


def test_process_inbox_shows_all_three_outcomes(bundle: APBundle, provider: LLMProvider) -> None:
    result = scenario_process_inbox(bundle, provider)
    amounts = {p["amount"] for p in result.payments}
    assert 800.0 in amounts            # small invoice → allowed + paid
    assert 6_000.0 not in amounts      # mid-size → held for approval (not dispatched)
    assert 500.0 not in amounts        # sanctioned vendor → denied (never paid)
    decs = {d["decision"] for d in result.decisions}
    assert {"allow", "hold", "deny"} <= decs


def test_blocked_is_directly_denied(bundle: APBundle, provider: LLMProvider) -> None:
    result = scenario_blocked(bundle, provider)
    # the gateway refuses it itself (no human) on the denylist gate
    assert any(d["decision"] == "deny" and "denylist" in d["rule"] for d in result.decisions)
    assert bundle.ledger.payments() == []  # type: ignore[attr-defined]


def test_approval_holds_then_releases(bundle: APBundle, provider: LLMProvider) -> None:
    result = scenario_approval(bundle, provider)
    assert any(d["decision"] == "hold" for d in result.decisions)
    pending = result.extra["pending"]
    assert len(pending) == 1
    assert approve_and_settle(bundle, pending[0].id) == 1
    assert len(bundle.ledger.payments()) == 1  # type: ignore[attr-defined]


def test_reject_never_pays_and_is_audited(bundle: APBundle, provider: LLMProvider) -> None:
    events: list[dict[str, object]] = []
    bundle.trace.subscribe(lambda e: events.append(dict(e)))
    result = scenario_approval(bundle, provider)
    pending = result.extra["pending"]
    bundle.reject(pending[0].id, "mgr-1")
    assert bundle.drain() == 0
    assert bundle.ledger.payments() == []  # type: ignore[attr-defined]
    # the rejection is a visible terminal outcome: audited DENY + a trace event
    audit = bundle.audit_reader.by_correlation("approval")
    assert any(r.decision.value == "deny" and r.action == "pay" for r in audit), "rejection audited"
    assert any(e.get("decision") == "deny" and "rejected" in str(e.get("rule")) for e in events), "rejection traced"


def test_audit_records_allow_and_hold(bundle: APBundle, provider: LLMProvider) -> None:
    scenario_process_inbox(bundle, provider)
    records = bundle.audit_reader.by_correlation("inbox")
    decisions = {r.decision.value for r in records}
    assert "allow" in decisions and "hold" in decisions
    for r in records:  # every record carries the required RFC §11 fields
        assert r.resource and r.action and r.decision and r.timestamp
