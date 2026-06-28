"""M2 — the store-backed counter gates (RFC §7.1/§7.2/§7.4/§7.11, design §6).

Acceptance C2 (rate, sliding window, per target) and C5 (quantityCap, per
subject). The same sliding-window semantics are re-verified against real Redis in
``test_m2_redis_integration.py``; here the backing store is the in-memory fake so
the logic is tested without Docker. A store that raises ⇒ fail-closed (design §13).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from acp_core.enums import Outcome
from acp_core.gating import RequestEnv
from acp_store import InMemoryCounterStore
from acp_gates.gates import quantity_cap, quota, rate, spend_limit
from tests.conftest import gate_ctx

T0 = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)


# --- C2 rate, sliding window, per target ---------------------------------
def test_c2_rate_per_target() -> None:
    store = InMemoryCounterStore()
    cfg = {"limit": "3/day", "per": "resource.customerId"}

    def attempt(customer: str, when: datetime) -> Outcome:
        env = RequestEnv(now=when, resource={"customerId": customer})
        return rate(cfg, gate_ctx("Payment", "pay", env=env, counters=store)).outcome

    assert attempt("C", T0) is Outcome.PASS
    assert attempt("C", T0 + timedelta(hours=1)) is Outcome.PASS
    assert attempt("C", T0 + timedelta(hours=2)) is Outcome.PASS
    assert attempt("C", T0 + timedelta(hours=3)) is Outcome.FAIL  # 4th within 24h
    # a different customer's first charge still passes
    assert attempt("D", T0 + timedelta(hours=3)) is Outcome.PASS


def test_rate_window_slides() -> None:
    store = InMemoryCounterStore()
    cfg = {"limit": "2/hour"}

    def attempt(when: datetime) -> Outcome:
        return rate(cfg, gate_ctx("Email", "sendEmail", env=RequestEnv(now=when), counters=store)).outcome

    assert attempt(T0) is Outcome.PASS
    assert attempt(T0 + timedelta(minutes=10)) is Outcome.PASS
    assert attempt(T0 + timedelta(minutes=20)) is Outcome.FAIL  # 3rd within the hour
    # two hours later every earlier hit has aged out of the 1-hour window
    assert attempt(T0 + timedelta(hours=2)) is Outcome.PASS


# --- quota (cumulative) --------------------------------------------------
def test_quota_cumulative() -> None:
    store = InMemoryCounterStore()

    def attempt(when: datetime) -> Outcome:
        return quota("3/day", gate_ctx("Export", "exportData", env=RequestEnv(now=when), counters=store)).outcome

    assert attempt(T0) is Outcome.PASS
    assert attempt(T0 + timedelta(hours=1)) is Outcome.PASS
    assert attempt(T0 + timedelta(hours=2)) is Outcome.PASS
    assert attempt(T0 + timedelta(hours=3)) is Outcome.FAIL


# --- C5 quantityCap, per subject -----------------------------------------
def test_c5_quantity_cap_per_subject() -> None:
    store = InMemoryCounterStore()
    cfg = {"per": "resource.patientId", "limit": 3, "window": "24h"}

    def attempt(patient: str, when: datetime) -> Outcome:
        env = RequestEnv(now=when, resource={"patientId": patient})
        return quantity_cap(cfg, gate_ctx("Patient", "administer", env=env, counters=store)).outcome

    for i in range(3):
        assert attempt("P", T0 + timedelta(hours=i)) is Outcome.PASS
    assert attempt("P", T0 + timedelta(hours=4)) is Outcome.FAIL  # 4th for patient P
    assert attempt("Q", T0 + timedelta(hours=4)) is Outcome.PASS  # patient Q's first


# --- spendLimit (per session) --------------------------------------------
def test_spend_limit_accumulates_per_session() -> None:
    store = InMemoryCounterStore()

    def attempt(cost: float, when: datetime) -> Outcome:
        return spend_limit("25/session", gate_ctx("Payment", "pay", env=RequestEnv(now=when, cost=cost), counters=store)).outcome

    assert attempt(10, T0) is Outcome.PASS  # 10
    assert attempt(10, T0 + timedelta(seconds=1)) is Outcome.PASS  # 20
    assert attempt(10, T0 + timedelta(seconds=2)) is Outcome.FAIL  # 30 > 25


# --- fail closed when the store is unreachable (design §13) --------------
class _BrokenStore:
    def hit(self, key: str, now: float, window_s: float) -> int:
        raise RuntimeError("counter store unreachable")

    def add(self, key: str, amount: float, now: float, window_s: float) -> float:
        raise RuntimeError("counter store unreachable")


def test_counter_store_down_fails_closed() -> None:
    broken: Any = _BrokenStore()
    out = quota("3/day", gate_ctx("Export", "exportData", env=RequestEnv(now=T0), counters=broken))
    assert out.outcome is Outcome.FAIL
    out2 = spend_limit("25/session", gate_ctx("Payment", "pay", env=RequestEnv(now=T0, cost=1), counters=broken))
    assert out2.outcome is Outcome.FAIL
