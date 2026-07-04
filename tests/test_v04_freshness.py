"""v0.4 CS-017 — decision freshness (changeset docs/RFC-changeset-v0.3-to-v0.4.md).

Acceptance D5 (decision TTL cancels a stale staged effect; a late approval does
not resurrect it) and D6 (volatile gates re-validated at dispatch; non-volatile
gates are NOT re-run). Driven through the in-memory outbox + dispatch worker;
the Postgres claim path is exercised in ``test_m4_pg_integration.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from stonefold_core import (
    Actor,
    ApprovalError,
    Decision,
    FreshnessConfig,
    InMemoryAuditSink,
    PendingState,
    RawCall,
    RequestEnv,
    Session,
    enforce,
    load_policy,
)
from stonefold_core.freshness import STALE_DECISION, VOLATILE_GATES, stale_guard_reason
from stonefold_connectors import InMemoryConnector
from stonefold_gates.engine import DefaultGateEngine, make_dispatch_revalidator
from stonefold_store import DispatchWorker, InMemoryOutboxStore
from tests.conftest import full_registry, load_schema

T0 = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
CFG = FreshnessConfig(
    default_ttl=timedelta(hours=24), irreversible_ttl=timedelta(minutes=30)
)


@dataclass
class Harness:
    reg: Any
    policy: Any
    audit: InMemoryAuditSink
    outbox: InMemoryOutboxStore
    engine: DefaultGateEngine
    effect_conn: InMemoryConnector

    def enforce(
        self,
        resource: str,
        action: str,
        data: dict[str, Any] | None = None,
        *,
        now: datetime | None = T0,
        freshness: FreshnessConfig | None = CFG,
    ) -> Any:
        return enforce(
            RawCall(resource=resource, action=action, data=data or {}),
            Actor(id="alice"),
            Session(id="s1", correlation_id="corr-1"),
            registry=self.reg,
            audit=self.audit,
            policy=self.policy,
            gates=self.engine,
            outbox=self.outbox,
            env=RequestEnv(now=now) if now is not None else None,
            freshness=freshness,
        )

    def worker_at(self, now: datetime) -> DispatchWorker:
        """A dispatch worker whose clock and volatile re-validation see ``now``."""
        connectors_map = {"email": self.effect_conn, "sql": self.effect_conn,
                          "in_memory": self.effect_conn}
        from stonefold_core import Connectors

        return DispatchWorker(
            self.outbox,
            Connectors(connectors_map),
            registry=self.reg,
            clock=lambda: now,
            revalidate=make_dispatch_revalidator(self.engine, self.policy),
        )

    def get(self, ticket: str) -> Any:
        row = self.outbox.get(ticket)
        assert row is not None
        return row


def harness(doc: dict[str, Any]) -> Harness:
    reg = full_registry()
    policy = load_policy(doc, reg, schema=load_schema())
    audit = InMemoryAuditSink()
    outbox = InMemoryOutboxStore(audit=audit)
    return Harness(reg, policy, audit, outbox, DefaultGateEngine(reg), InMemoryConnector())


def _approval_doc() -> dict[str, Any]:
    return {
        "agent": "support",
        "allow": [{"effect": ["sendEmail"]}],
        "gates": {"sendEmail": {"requireApproval": {"approvers": "role:finance"}}},
    }


def _denylist_doc() -> dict[str, Any]:
    return {
        "agent": "pay",
        "allow": [{"effect": ["pay"]}],
        "gates": {"pay": {"denylist": {"field": "data.country", "set": "sanctioned-list"}}},
    }


# --- D5: decision TTL cancels a stale staged effect -----------------------
def test_d5_expired_row_settles_stale_decision_never_dispatched() -> None:
    h = harness(_approval_doc())
    result = h.enforce("Email", "sendEmail", {"to": "x@acme.example"})
    assert result.decision is Decision.HOLD
    row = h.get(result.ticket)
    # irreversible effect ⇒ the SHORT TTL applies, stamped from the injected clock
    assert row.expires_at == T0 + timedelta(minutes=30)

    # the approval arrives late — it promotes the row, but cannot outrun the TTL
    h.outbox.approve(result.ticket, "boss")
    assert h.get(result.ticket).state is PendingState.PENDING

    worker = h.worker_at(T0 + timedelta(hours=1))
    worker.drain()
    settled = h.get(result.ticket)
    assert settled.state is PendingState.CANCELLED
    assert settled.reason == STALE_DECISION
    assert h.effect_conn.effects == []  # nothing was dispatched

    # the cancellation is audited (changeset CS-017: "audited")
    last = h.audit.records[-1]
    assert last.decision is Decision.DENY
    assert last.outcome == "cancelled"
    # a later approval does not resurrect the cancelled row
    with pytest.raises(ApprovalError):
        h.outbox.approve(result.ticket, "boss2")


def test_d5_claim_within_ttl_dispatches_normally() -> None:
    h = harness(_approval_doc())
    result = h.enforce("Email", "sendEmail", {"to": "x@acme.example"})
    h.outbox.approve(result.ticket, "boss")

    assert h.worker_at(T0 + timedelta(minutes=5)).drain() == 1
    assert h.get(result.ticket).state is PendingState.DONE
    assert len(h.effect_conn.effects) == 1


def test_d5_default_ttl_applies_to_non_irreversible_effects() -> None:
    h = harness({"agent": "pay", "allow": [{"effect": ["pay"]}]})
    result = h.enforce("Payment", "pay", {"amount": 1, "country": "FR"})
    assert result.decision is Decision.ALLOW
    assert h.get(result.ticket).expires_at == T0 + timedelta(hours=24)


def test_d5_expired_row_does_not_block_the_queue() -> None:
    # An expired row is cancelled inside the claim and the scan continues to the
    # next PENDING row — one run_once handles both.
    h = harness({"agent": "pay", "allow": [{"effect": ["pay"]}]})
    stale = h.enforce("Payment", "pay", {"amount": 1})
    fresh = h.enforce("Payment", "pay", {"amount": 2}, now=T0 + timedelta(hours=23))

    worker = h.worker_at(T0 + timedelta(hours=25))  # stale expired, fresh is not
    assert worker.run_once() is True
    assert h.get(stale.ticket).state is PendingState.CANCELLED
    assert h.get(stale.ticket).reason == STALE_DECISION
    assert h.get(fresh.ticket).state is PendingState.DONE
    assert len(h.effect_conn.effects) == 1


def test_freshness_without_clock_fails_closed_at_staging() -> None:
    # Freshness configured but no injected clock ⇒ the gateway cannot bound the
    # decision's validity ⇒ deny, and nothing is staged (invariant 7).
    h = harness({"agent": "pay", "allow": [{"effect": ["pay"]}]})
    result = h.enforce("Payment", "pay", {"amount": 1}, now=None)
    assert result.decision is Decision.DENY
    assert result.rule == "freshness-unavailable"
    assert h.outbox.list_by_state(PendingState.PENDING) == []


def test_no_freshness_config_means_no_expiry() -> None:
    # v0.3 behaviour is preserved when freshness is not configured (opt-in).
    h = harness({"agent": "pay", "allow": [{"effect": ["pay"]}]})
    result = h.enforce("Payment", "pay", {"amount": 1}, freshness=None)
    assert h.get(result.ticket).expires_at is None
    assert h.worker_at(T0 + timedelta(days=365)).drain() == 1
    assert h.get(result.ticket).state is PendingState.DONE


# --- D6: volatile gates re-validated at dispatch ---------------------------
def test_d6_denylist_updated_between_decision_and_dispatch() -> None:
    h = harness(_denylist_doc())
    result = h.enforce("Payment", "pay", {"amount": 100, "country": "FR"})
    assert result.decision is Decision.ALLOW  # FR not sanctioned at decision time

    # the destination country is sanctioned before dispatch
    h.reg.file.sets["sanctioned-list"] = ("KP", "IR", "SY", "CU", "FR")

    h.worker_at(T0 + timedelta(minutes=5)).drain()
    settled = h.get(result.ticket)
    assert settled.state is PendingState.CANCELLED
    assert settled.reason == stale_guard_reason("denylist")
    assert h.effect_conn.effects == []  # never a partial dispatch
    last = h.audit.records[-1]
    assert last.decision is Decision.DENY
    assert last.outcome == "cancelled"

    # a fresh submission of the same call is now denied at decision time
    again = h.enforce("Payment", "pay", {"amount": 100, "country": "FR"})
    assert again.decision is Decision.DENY
    assert again.rule == "gate:denylist"


def test_d6_passing_volatile_gates_dispatch_normally() -> None:
    h = harness(_denylist_doc())
    result = h.enforce("Payment", "pay", {"amount": 100, "country": "FR"})
    assert h.worker_at(T0 + timedelta(minutes=5)).drain() == 1
    assert h.get(result.ticket).state is PendingState.DONE


def test_d6_counter_gates_are_not_rerun_at_dispatch() -> None:
    # rate 1/hour was consumed at decision time; re-running it at dispatch would
    # double-count (2 > 1) and wrongly cancel. Non-volatile gates stay decided.
    doc = {
        "agent": "support",
        "allow": [{"effect": ["sendEmail"]}],
        "gates": {"sendEmail": {"rate": "1/hour"}},
    }
    h = harness(doc)
    result = h.enforce("Email", "sendEmail", {"to": "x@acme.example"})
    assert result.decision is Decision.ALLOW

    assert h.worker_at(T0 + timedelta(minutes=5)).drain() == 1
    assert h.get(result.ticket).state is PendingState.DONE


def test_d6_approvals_are_not_rerequested_at_dispatch() -> None:
    # The grant IS the release; its freshness is bounded by the TTL, not by a
    # re-run of the approval gate.
    h = harness(_approval_doc())
    result = h.enforce("Email", "sendEmail", {"to": "x@acme.example"})
    h.outbox.approve(result.ticket, "boss")
    assert h.worker_at(T0 + timedelta(minutes=5)).drain() == 1
    assert h.get(result.ticket).state is PendingState.DONE


def test_revalidate_volatile_skips_non_volatile_gates() -> None:
    # A rate of 0/hour fails on ANY hit — revalidate_volatile returning None
    # proves the counter gate was never invoked.
    reg = full_registry()
    doc = {
        "agent": "support",
        "allow": [{"effect": ["sendEmail"]}],
        "gates": {"sendEmail": {"rate": "0/hour"}},
    }
    policy = load_policy(doc, reg, schema=load_schema())
    engine = DefaultGateEngine(reg)
    resolved = reg.resolve(RawCall(resource="Email", action="sendEmail", data={"to": "x"}))
    failing = engine.revalidate_volatile(
        resolved, Actor(id="alice"), Session(id="s1"), policy, RequestEnv(now=T0)
    )
    assert failing is None


# --- FreshnessConfig unit checks -------------------------------------------
def test_freshness_config_ttl_selection() -> None:
    reg = full_registry()
    irreversible = reg.resolve(RawCall(resource="Email", action="sendEmail", data={}))
    compensable = reg.resolve(RawCall(resource="Payment", action="pay", data={}))
    assert CFG.ttl_for(irreversible) == timedelta(minutes=30)
    assert CFG.ttl_for(compensable) == timedelta(hours=24)
    assert CFG.expiry_for(irreversible, T0) == T0 + timedelta(minutes=30)


def test_freshness_config_rejects_non_finite_ttls() -> None:
    with pytest.raises(ValueError):
        FreshnessConfig(default_ttl=timedelta(0))
    with pytest.raises(ValueError):
        FreshnessConfig(irreversible_ttl=timedelta(seconds=-1))


def test_volatile_gate_set_is_the_specified_five() -> None:
    # CS-017 freezes the volatile/non-volatile split; a drift here is a spec bug.
    assert VOLATILE_GATES == {
        "allowlist", "denylist", "window", "precondition", "emissionControl"
    }
