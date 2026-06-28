"""M0 — value model unit tests (design §2; plan M0 task 2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from acp_core import (
    Actor,
    Attributes,
    Decision,
    Emission,
    EvalResult,
    Explainability,
    GateResult,
    Kind,
    OperativeForce,
    Outcome,
    RawCall,
    ResolvedAction,
    Reversibility,
    Session,
)


def test_kind_enum_values() -> None:
    assert {k.value for k in Kind} == {
        "observe",
        "assess",
        "record",
        "effect",
        "transition",
    }


def test_decision_and_outcome_enums() -> None:
    assert {d.value for d in Decision} == {"allow", "hold", "deny", "halt"}
    assert {o.value for o in Outcome} == {"pass", "fail", "hold"}


def test_attribute_enums_match_rfc_section_5() -> None:
    assert {r.value for r in Reversibility} == {
        "reversible",
        "compensable",
        "irreversible",
    }
    assert {e.value for e in Emission} == {"none", "emits"}
    assert {o.value for o in OperativeForce} == {"none", "low", "high"}
    assert {e.value for e in Explainability} == {"none", "required"}


def test_attributes_defaults_are_conservative() -> None:
    a = Attributes()
    assert a.reversibility is Reversibility.REVERSIBLE
    assert a.emission is Emission.NONE
    assert a.operativeForce is OperativeForce.NONE
    assert a.resultSensitivity == "internal"
    assert a.explainability is Explainability.NONE


def test_value_types_are_frozen() -> None:
    a = Actor(id="alice")
    with pytest.raises(ValidationError):
        a.id = "mallory"  # type: ignore[misc]


def test_rawcall_has_no_actor_field() -> None:
    """Invariant 3: the agent payload cannot carry identity. Even if the agent
    stuffs owner_id into data, it remains opaque parameters — there is no
    actor/owner/tenant attribute on RawCall the gateway would read for scope."""
    call = RawCall(resource="Customer", action="read", data={"owner_id": "evil"})
    assert "actor" not in RawCall.model_fields
    assert call.data["owner_id"] == "evil"  # opaque, never used for identity


def test_resolved_action_round_trips() -> None:
    ra = ResolvedAction(
        kind=Kind.EFFECT,
        resource="Email",
        action="sendEmail",
        data={"to": "x@acme.example"},
        attrs=Attributes(reversibility=Reversibility.COMPENSABLE),
        connector="email",
    )
    assert ra.kind is Kind.EFFECT
    assert ra.from_states == ()


def test_eval_and_gate_result() -> None:
    g = GateResult(gate="valueLimit", outcome=Outcome.FAIL, reason="over max")
    er = EvalResult(decision=Decision.DENY, rule="valueLimit", gates=(g,))
    assert er.decision is Decision.DENY
    assert er.gates[0].outcome is Outcome.FAIL


def test_session_correlation() -> None:
    s = Session(id="sess-1")
    assert s.correlation_id is None
