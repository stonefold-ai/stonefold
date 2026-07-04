"""M5 — the kill check in the enforcement pipeline (RFC §9/§12 step 5, design §8.3).

Acceptance **E1** (a kill turns subsequent actions into audited ``HALT``, and
retries keep HALTing) and **E5** (kill store unreachable ⇒ fail closed for an
irreversible effect). Also covers the three scope kinds reaching the right check
point and that ``HALT`` is distinct from ``DENY``.
"""

from __future__ import annotations

from typing import Any

import pytest

from stonefold_core import (
    Actor,
    Connectors,
    Decision,
    InMemoryAuditSink,
    KillScope,
    PendingState,
    RawCall,
    Session,
    enforce,
    load_policy,
)
from stonefold_connectors import InMemoryConnector
from stonefold_gates.engine import DefaultGateEngine
from stonefold_store import InMemoryOutboxStore
from stonefold_store.kill_memory import InMemoryKillStore
from tests.conftest import full_registry, load_schema


def _enforce(doc: dict[str, Any], *, kill: Any, resource: str, action: str,
             actor: Actor | None = None, session: str = "s1",
             data: dict[str, Any] | None = None) -> Any:
    reg = full_registry()
    policy = load_policy(doc, reg, schema=load_schema())
    audit = InMemoryAuditSink()
    outbox = InMemoryOutboxStore(audit=audit)
    connectors = Connectors({"in_memory": InMemoryConnector(), "email": InMemoryConnector(),
                             "sql": InMemoryConnector()})
    result = enforce(
        RawCall(resource=resource, action=action, data=data or {}),
        actor or Actor(id="alice"),
        Session(id=session, correlation_id="corr-1"),
        registry=reg, audit=audit, policy=policy,
        gates=DefaultGateEngine(reg), outbox=outbox,
        connectors=connectors, kill=kill,
    )
    return result, audit, outbox


# --- E1: session kill ⇒ HALT, audited, repeatable ------------------------
def test_e1_session_kill_halts_subsequent_actions() -> None:
    kill = InMemoryKillStore()
    kill.issue(KillScope.for_session("s1"), issued_by="operator")
    doc = {"agent": "support", "allow": [{"observe": ["read"]}, {"effect": ["sendEmail"]}]}

    result, audit, outbox = _enforce(doc, kill=kill, resource="Customer", action="read",
                                     session="s1")
    assert result.decision is Decision.HALT  # a distinct terminal state, not DENY
    # audited as halt
    assert any(r.decision is Decision.HALT for r in audit.records)
    assert not any(r.decision is Decision.DENY for r in audit.records)


def test_e1_retry_keeps_halting_and_stages_nothing() -> None:
    kill = InMemoryKillStore()
    kill.issue(KillScope.for_session("s1"), issued_by="operator")
    doc = {"agent": "support", "allow": [{"effect": ["sendEmail"]}]}

    for _ in range(3):
        result, _audit, outbox = _enforce(doc, kill=kill, resource="Email",
                                          action="sendEmail", session="s1",
                                          data={"to": "x@acme.example"})
        assert result.decision is Decision.HALT
        # nothing staged for dispatch — the effect never reaches the outbox
        assert outbox.list_by_state(PendingState.PENDING) == []
        assert outbox.list_by_state(PendingState.PENDING_APPROVAL) == []


def test_other_session_is_unaffected() -> None:
    kill = InMemoryKillStore()
    kill.issue(KillScope.for_session("s1"), issued_by="operator")
    doc = {"agent": "support", "allow": [{"observe": ["read"]}]}

    result, _a, _o = _enforce(doc, kill=kill, resource="Customer", action="read",
                              session="s2")
    assert result.decision is Decision.ALLOW


# --- the scope kinds reach the right check point -------------------------
def test_global_kill_halts_everything() -> None:
    kill = InMemoryKillStore()
    kill.issue(KillScope.for_global(), issued_by="operator")
    doc = {"agent": "support", "allow": [{"observe": ["read"]}]}
    result, _a, _o = _enforce(doc, kill=kill, resource="Customer", action="read")
    assert result.decision is Decision.HALT


def test_agent_kill_halts_that_agent() -> None:
    kill = InMemoryKillStore()
    kill.issue(KillScope.for_agent("support"), issued_by="operator")
    doc = {"agent": "support", "allow": [{"observe": ["read"]}]}
    result, _a, _o = _enforce(doc, kill=kill, resource="Customer", action="read")
    assert result.decision is Decision.HALT


def test_action_class_kill_halts_only_matching_action() -> None:
    # ACTION_CLASS is matched at step 5 (needs the resolved kind/resource/action).
    kill = InMemoryKillStore()
    kill.issue(KillScope.for_action_class(resource="Payment", action="pay"),
               issued_by="operator")
    doc = {"agent": "pay", "allow": [{"effect": ["pay", "refund"]}]}

    halted, _a, _o = _enforce(doc, kill=kill, resource="Payment", action="pay",
                              data={"amount": 1})
    assert halted.decision is Decision.HALT

    allowed, _a2, o2 = _enforce(doc, kill=kill, resource="Payment", action="refund",
                                data={"amount": 1})
    assert allowed.decision is Decision.ALLOW  # different action, not killed


# --- E5: kill store unreachable ⇒ fail closed for irreversible -----------
class _BrokenKillStore:
    """A kill store whose hot-path read fails (Redis/Postgres down)."""

    def matches(self, target: Any) -> Any:
        raise RuntimeError("kill store unreachable")

    def issue(self, *a: Any, **k: Any) -> Any:  # pragma: no cover - unused
        raise RuntimeError("unreachable")

    def lift(self, *a: Any, **k: Any) -> Any:  # pragma: no cover - unused
        raise RuntimeError("unreachable")

    def active_orders(self) -> Any:  # pragma: no cover - unused
        return ()

    def epoch(self) -> int:  # pragma: no cover - unused
        return 0


def test_e5_irreversible_effect_fails_closed_when_kill_store_down() -> None:
    doc = {"agent": "rx", "allow": [{"effect": ["prescribe"]}]}
    result, _a, outbox = _enforce(doc, kill=_BrokenKillStore(), resource="Prescribing",
                                  action="prescribe", data={"drug": "X"})
    # an unreadable kill must not be assumed absent for an irreversible effect
    assert result.decision in (Decision.HALT, Decision.DENY)
    assert outbox.list_by_state(PendingState.PENDING) == []  # never staged


def test_e5_default_closed_policy_fails_closed_for_reversible_too() -> None:
    # With the default failureMode (closed), a reversible action also fails closed
    # when the kill store is unreadable.
    doc = {"agent": "support", "allow": [{"effect": ["sendEmail"]}]}
    result, _a, _o = _enforce(doc, kill=_BrokenKillStore(), resource="Email",
                              action="sendEmail", data={"to": "x@acme.example"})
    assert result.decision in (Decision.HALT, Decision.DENY)
