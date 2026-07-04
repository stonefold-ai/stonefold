"""M2 — the gate engine wired through the pipeline (RFC §7/§12 step 4, design §6).

Covers AND-combination, the FAIL⇒DENY / HOLD⇒HOLD precedence, the ordering
guarantee (a cheap FAIL short-circuits *before* an approval HOLD is ever raised),
the built-in transition guard, and acceptance C8 (a gate's ``when:`` that can't
resolve fails the gate closed — distinct from the condition being false).
"""

from __future__ import annotations

from typing import Any

from stonefold_core import (
    Actor,
    Decision,
    InMemoryAuditSink,
    RawCall,
    Session,
    enforce,
    load_policy,
)
from stonefold_core.enums import Outcome
from stonefold_core.gating import RequestEnv
from stonefold_gates.engine import DefaultGateEngine
from tests.conftest import full_registry, load_schema


def run(
    doc: dict[str, Any],
    resource: str,
    action: str,
    *,
    data: dict[str, Any] | None = None,
    env: RequestEnv | None = None,
) -> Any:
    reg = full_registry()
    policy = load_policy(doc, reg, schema=load_schema())
    engine = DefaultGateEngine(reg)
    audit = InMemoryAuditSink()
    result = enforce(
        RawCall(resource=resource, action=action, data=data or {}),
        Actor(id="alice"),
        Session(id="s1"),
        registry=reg,
        audit=audit,
        policy=policy,
        gates=engine,
        env=env or RequestEnv(),
    )
    return result, audit


def test_passing_gate_allows() -> None:
    doc = {
        "agent": "pay",
        "allow": [{"effect": ["pay"]}],
        "gates": {"pay": {"valueLimit": {"field": "data.amount", "max": 10000}}},
    }
    result, _ = run(doc, "Payment", "pay", data={"amount": 5000})
    assert result.decision is Decision.ALLOW
    assert result.rule == "allow"


def test_failing_gate_denies_with_gate_rule() -> None:
    doc = {
        "agent": "pay",
        "allow": [{"effect": ["pay"]}],
        "gates": {"pay": {"valueLimit": {"field": "data.amount", "max": 10000}}},
    }
    result, audit = run(doc, "Payment", "pay", data={"amount": 10001})
    assert result.decision is Decision.DENY
    assert result.rule == "gate:valueLimit"
    # the refusal is audited with the gate trace (invariant 6)
    assert audit.records[-1].decision is Decision.DENY
    assert any(g.gate == "valueLimit" for g in audit.records[-1].gates)


def test_gates_are_anded() -> None:
    # valueLimit passes but denylist fails ⇒ overall DENY (every gate must pass).
    doc = {
        "agent": "pay",
        "allow": [{"effect": ["pay"]}],
        "gates": {
            "pay": {
                "valueLimit": {"field": "data.amount", "max": 10000},
                "denylist": {"field": "data.destinationCountry", "set": "sanctioned-list"},
            }
        },
    }
    result, _ = run(doc, "Payment", "pay", data={"amount": 100, "destinationCountry": "KP"})
    assert result.decision is Decision.DENY
    assert result.rule == "gate:denylist"


def test_require_approval_holds() -> None:
    doc = {
        "agent": "pay",
        "allow": [{"effect": ["pay"]}],
        "gates": {"pay": {"requireApproval": {"approvers": "role:finance"}}},
    }
    result, _ = run(doc, "Payment", "pay", data={"amount": 1})
    assert result.decision is Decision.HOLD
    assert result.rule == "gate:requireApproval"


def test_fail_short_circuits_before_approval() -> None:
    # DoD ordering: a cheap FAIL is reached before the (expensive) approval gate,
    # so the approval is never evaluated.
    doc = {
        "agent": "pay",
        "allow": [{"effect": ["pay"]}],
        "gates": {
            "pay": {
                "valueLimit": {"field": "data.amount", "max": 100},
                "requireApproval": {"approvers": "role:finance"},
            }
        },
    }
    result, _ = run(doc, "Payment", "pay", data={"amount": 10000})
    assert result.decision is Decision.DENY
    assert result.rule == "gate:valueLimit"
    evaluated = {g.gate for g in result.gates}
    assert "requireApproval" not in evaluated  # approval never ran


def test_kind_and_action_gates_combine() -> None:
    # A3-style: a kind-level gate AND an action-level gate both apply.
    doc = {
        "agent": "pay",
        "allow": [{"effect": ["pay"]}],
        "gates": {
            "effect": {"valueLimit": {"field": "data.amount", "max": 10000}},
            "pay": {"denylist": {"field": "data.destinationCountry", "set": "sanctioned-list"}},
        },
    }
    ok, _ = run(doc, "Payment", "pay", data={"amount": 100, "destinationCountry": "US"})
    assert ok.decision is Decision.ALLOW
    over, _ = run(doc, "Payment", "pay", data={"amount": 99999, "destinationCountry": "US"})
    assert over.decision is Decision.DENY  # kind-level valueLimit fails


def test_builtin_transition_from_states_without_explicit_gate() -> None:
    # RFC §7.6: a transition always re-checks its declared from-states, even with
    # no precondition gate in the policy.
    doc = {"agent": "legal", "allow": [{"transition": {"Matter": ["engage"]}}]}
    denied, _ = run(doc, "Matter", "engage", env=RequestEnv(resource={"currentState": "active"}))
    assert denied.decision is Decision.DENY
    allowed, _ = run(doc, "Matter", "engage", env=RequestEnv(resource={"currentState": "conflict_check"}))
    assert allowed.decision is Decision.ALLOW


def test_c8_when_fail_closed_vs_condition_false() -> None:
    doc = {
        "agent": "pay",
        "allow": [{"effect": ["pay"]}],
        "gates": {"pay": {"requireApproval": {"when": "resource.foo == 1"}}},
    }
    # resource.foo absent ⇒ the gate's condition can't resolve ⇒ fail-closed DENY
    missing, _ = run(doc, "Payment", "pay", env=RequestEnv(resource={}))
    assert missing.decision is Decision.DENY

    # condition resolves to false ⇒ gate inactive ⇒ ALLOW (distinct from above)
    false_case, _ = run(doc, "Payment", "pay", env=RequestEnv(resource={"foo": 2}))
    assert false_case.decision is Decision.ALLOW

    # condition true ⇒ approval required ⇒ HOLD
    true_case, _ = run(doc, "Payment", "pay", env=RequestEnv(resource={"foo": 1}))
    assert true_case.decision is Decision.HOLD


def test_no_engine_skips_gate_stage() -> None:
    # Back-compat: with no engine injected, authorization alone decides (M1).
    reg = full_registry()
    doc = {
        "agent": "pay",
        "allow": [{"effect": ["pay"]}],
        "gates": {"pay": {"valueLimit": {"field": "data.amount", "max": 1}}},
    }
    policy = load_policy(doc, reg, schema=load_schema())
    result = enforce(
        RawCall(resource="Payment", action="pay", data={"amount": 999}),
        Actor(id="alice"),
        Session(id="s1"),
        registry=reg,
        audit=InMemoryAuditSink(),
        policy=policy,
    )
    assert result.decision is Decision.ALLOW  # gate not evaluated without an engine
