"""Core pipeline behaviour for the Accounts-Payable demo (acceptance §G, B2, E1).

Drives the *real* enforcement stack over the unmodified ``payments-ops.stele.yaml``
through ``APBundle.submit`` — exactly what the agent's gated tool calls — with an
in-memory ledger and a fixed clock. No LLM and no Docker: this isolates the
gateway behaviour the higher layers (agent, UI, compose) rely on.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from stonefold_core import Decision, EvalResult, KillScope
from stonefold_ap_demo.gateway import APBundle, build_inmemory_bundle
from stonefold_ap_demo.principals import AP_OPERATOR, OUT_OF_TENANT_OPERATOR, PAYMENTS_MANAGER

DEMO_NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def bundle() -> APBundle:
    return build_inmemory_bundle(clock=lambda: DEMO_NOW)


def _pay(bundle: APBundle, data: dict[str, object], *, actor: str = AP_OPERATOR,
         session: str = "s1") -> EvalResult:
    return bundle.submit(
        actor_id=actor, resource="Payment", action="pay", data=data,
        session_id=session, correlation_id=session,
    )


def _acme_800() -> dict[str, object]:
    # vendorId + sourceDomain are the v0.6 requireMatch inputs: the payment
    # must correspond to the vendor's open purchase order (RFC §7.16).
    return {"payeeId": "PE-ACME-SUP", "accountId": "ACME-OPS", "amount": 800.0,
            "currency": "USD", "destinationCountry": "GB", "invoiceId": "INV-1001",
            "vendorId": "PE-ACME-SUP", "sourceDomain": "acme.example"}


# --- G1 happy path -------------------------------------------------------------
def test_g1_happy_path_pays(bundle: APBundle) -> None:
    result = _pay(bundle, _acme_800())
    assert result.decision is Decision.ALLOW
    assert result.ticket is not None  # staged via the outbox
    # nothing has actually moved until the worker dispatches
    assert bundle.ledger.payments() == []  # type: ignore[attr-defined]
    assert bundle.drain() == 1
    payments = bundle.ledger.payments()  # type: ignore[attr-defined]
    assert len(payments) == 1
    assert payments[0]["amount"] == 800.0
    assert payments[0]["payee_id"] == "PE-ACME-SUP"


def test_g1_dispatch_is_idempotent(bundle: APBundle) -> None:
    _pay(bundle, _acme_800())
    assert bundle.drain() == 1
    assert bundle.drain() == 0  # nothing left PENDING ⇒ no double-send
    assert len(bundle.ledger.payments()) == 1  # type: ignore[attr-defined]


# --- CS-009: the settled effect's audit carries the downstream id (resultRef) ---
def test_executed_pay_audit_carries_result_ref(bundle: APBundle) -> None:
    result = _pay(bundle, _acme_800())
    assert result.decision is Decision.ALLOW
    assert bundle.drain() == 1
    pay_id = bundle.ledger.payments()[0]["id"]  # type: ignore[attr-defined]
    settle = [r for r in bundle.audit_records()
              if r.action == "pay" and r.outcome == "success"]
    assert settle and settle[0].resultRefs == [pay_id]  # the handle(s) to find/compensate it
    # a refusal creates no downstream record, so it carries no resultRefs
    denied = _pay(bundle, _acme_800() | {"destinationCountry": "KP"})
    assert denied.decision is Decision.DENY
    deny_recs = [r for r in bundle.audit_records()
                 if r.action == "pay" and r.decision is Decision.DENY]
    assert deny_recs and all(r.resultRefs == [] for r in deny_recs)


# --- observe is scoped below the model (B1-style) ------------------------------
def test_observe_account_is_tenant_scoped(bundle: APBundle) -> None:
    result = bundle.submit(actor_id=AP_OPERATOR, resource="Account", action="read",
                           data={}, session_id="s1")
    assert result.decision is Decision.ALLOW
    assert result.output is not None
    ids = {row["id"] for row in result.output}
    assert ids == {"ACME-OPS"}  # the rival tenant's account is invisible


# --- B2 effect scope: cannot pay from another tenant's account -----------------
def test_b2_pay_from_out_of_tenant_account_denied(bundle: APBundle) -> None:
    data = _acme_800() | {"accountId": "RIVAL-OPS"}
    result = _pay(bundle, data)
    assert result.decision is Decision.DENY
    assert result.rule == "scope-denied"
    assert bundle.drain() == 0  # never staged, never dispatched


# --- G2 the indirect-injection attack is refused -------------------------------
def test_g2_attack_new_payee_wire_denied(bundle: APBundle) -> None:
    attack = {"newPayee": "QuickPay Settlements", "iban": "GB91QUICK0000099999",
              "amount": 50_000.0, "currency": "USD", "destinationCountry": "GB",
              "accountId": "ACME-OPS"}
    result = _pay(bundle, attack)
    assert result.decision is Decision.DENY
    # v0.6: the fraudulent invoice is refused by *matching* — it corresponds to
    # no purchase order (it does not even carry the fields to match one). The
    # new-payee cooling-off precondition remains behind it as defence in depth.
    assert "requireMatch" in result.rule
    assert bundle.drain() == 0
    assert bundle.ledger.payments() == []  # type: ignore[attr-defined]


def test_g2_attack_with_forged_vendor_fields_still_denied(bundle: APBundle) -> None:
    # a smarter attacker copies a real vendor's id but the amount corresponds
    # to no open PO line — no obligation, no payment (the in-bounds wrong action).
    attack = {"newPayee": "QuickPay Settlements", "iban": "GB91QUICK0000099999",
              "amount": 50_000.0, "currency": "USD", "destinationCountry": "GB",
              "accountId": "ACME-OPS", "vendorId": "PE-ACME-SUP",
              "sourceDomain": "acme.example"}
    result = _pay(bundle, attack)
    assert result.decision is Decision.DENY
    assert "requireMatch" in result.rule
    assert result.reason_code == "outside-tolerance"
    assert bundle.ledger.payments() == []  # type: ignore[attr-defined]


def test_denylist_blocks_sanctioned_country(bundle: APBundle) -> None:
    data = _acme_800() | {"destinationCountry": "KP"}
    result = _pay(bundle, data)
    assert result.decision is Decision.DENY
    assert "denylist" in result.rule


def test_value_limit_blocks_oversized(bundle: APBundle) -> None:
    data = _acme_800() | {"amount": 2_000_000.0}
    result = _pay(bundle, data)
    assert result.decision is Decision.DENY
    assert "valueLimit" in result.rule


def test_export_is_default_denied(bundle: APBundle) -> None:
    result = bundle.submit(actor_id=AP_OPERATOR, resource="Payment", action="refund",
                           data={"amount": 10.0}, session_id="s1")
    # refund is not in payments-ops' allow set ⇒ default deny
    assert result.decision is Decision.DENY


# --- G3 approval in the loop ---------------------------------------------------
def test_g3_midsize_holds_then_approves(bundle: APBundle) -> None:
    data = {"payeeId": "PE-GLOBEX", "accountId": "ACME-OPS", "amount": 6_000.0,
            "currency": "USD", "destinationCountry": "US", "invoiceId": "INV-1002",
            "vendorId": "PE-GLOBEX", "sourceDomain": "globex.example"}
    result = _pay(bundle, data)
    assert result.decision is Decision.HOLD
    assert result.ticket is not None
    assert bundle.drain() == 0  # nothing dispatched while held

    pending = bundle.pending_approvals()
    assert len(pending) == 1 and pending[0].id == result.ticket

    bundle.approve(result.ticket, PAYMENTS_MANAGER)
    assert bundle.drain() == 1
    assert len(bundle.ledger.payments()) == 1  # type: ignore[attr-defined]


def test_g3_reject_never_pays(bundle: APBundle) -> None:
    data = {"payeeId": "PE-GLOBEX", "accountId": "ACME-OPS", "amount": 6_000.0,
            "currency": "USD", "destinationCountry": "US",
            "vendorId": "PE-GLOBEX", "sourceDomain": "globex.example"}
    result = _pay(bundle, data)
    assert result.decision is Decision.HOLD
    assert result.ticket is not None
    bundle.reject(result.ticket, PAYMENTS_MANAGER)
    assert bundle.drain() == 0
    assert bundle.ledger.payments() == []  # type: ignore[attr-defined]


# --- E1 kill turns subsequent actions into HALT --------------------------------
def test_e1_session_kill_halts(bundle: APBundle) -> None:
    # pre-kill action allowed
    assert _pay(bundle, _acme_800(), session="live").decision is Decision.ALLOW
    bundle.issue_kill(KillScope.for_session("live"), issued_by="operator")
    after = _pay(bundle, _acme_800(), session="live")
    assert after.decision is Decision.HALT
    # a different session is unaffected
    assert _pay(bundle, _acme_800(), session="other").decision is Decision.ALLOW


# --- identity comes from the directory, not the body (invariant 3) -------------
def test_unknown_principal_refused(bundle: APBundle) -> None:
    result = bundle.submit(actor_id="ghost", resource="Account", action="read",
                           data={}, session_id="s1")
    assert result.decision is Decision.DENY
    assert result.rule == "unknown-principal"


def test_out_of_tenant_operator_sees_no_acme_accounts(bundle: APBundle) -> None:
    result = bundle.submit(actor_id=OUT_OF_TENANT_OPERATOR, resource="Account",
                           action="read", data={}, session_id="s1")
    assert result.decision is Decision.ALLOW
    assert result.output is not None
    assert all(row["id"] != "ACME-OPS" for row in result.output)


# --- the trace bus emits decision + effect events ------------------------------
def test_trace_emits_decision_and_effect(bundle: APBundle) -> None:
    events: list[dict[str, object]] = []
    bundle.trace.subscribe(lambda e: events.append(dict(e)))
    _pay(bundle, _acme_800())
    bundle.drain()
    kinds = [e["type"] for e in events]
    assert "decision" in kinds and "effect" in kinds
