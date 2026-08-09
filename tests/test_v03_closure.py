"""v0.3 §? — the standard closure check (spec §7.6, registry docs/06 §5c).

The failure being closed: a gate refuses the work, and the actor then closes the
item as done. Four rules, one test each, plus the two lint directions and the
boundary this check must not cross.
"""

from __future__ import annotations

from typing import Any

import pytest

from stonefold_core import (
    Actor,
    Connectors,
    Decision,
    InMemoryAuditSink,
    RawCall,
    Session,
    enforce,
    load_policy,
)
from stonefold_core.enums import Outcome
from stonefold_core.linter import PolicyError, Severity, lint
from stonefold_core.policy import Policy
from stonefold_core.registry import load_registry
from stonefold_connectors import InMemoryConnector
from stonefold_gates.closure import (
    CLOSED_WITHOUT_THE_WORK,
    DISPOSITION_REQUIRED,
    AuditDecisionHistory,
)
from stonefold_gates.engine import DefaultGateEngine
from stonefold_store import InMemoryOutboxStore
from tests.conftest import load_schema

ACTOR = Actor(id="agent-7")
RUN = Session(id="s1", correlation_id="run-1")

# A worklist whose closing action declares what closing it means, and a payment
# action to be refused so there is something in the run's history.
REGISTRY = {
    "resources": {
        "Document": {
            "actions": {
                "read": {"kind": "observe"},
                "markWorked": {
                    "kind": "record",
                    "label": "Close a document as worked.",
                    "data": {
                        "documentId": {"type": "string", "required": True},
                        "disposition": {
                            "values": ["resolved", "escalated", "referred", "duplicate"],
                            "required": True,
                        },
                    },
                    "closure": {
                        "dispositionField": "disposition",
                        "claimsCompletion": ["resolved"],
                    },
                },
            }
        },
        "Supplier": {
            "actions": {
                "updateBankAccount": {"kind": "record", "label": "Change bank details."}
            }
        },
    },
    "preconditionChecks": [
        {
            "name": "dispositionIsDeclared",
            "holdCapable": True,
            "reasonCodes": {
                DISPOSITION_REQUIRED: "retryable",
                CLOSED_WITHOUT_THE_WORK: "escalate",
            },
        }
    ],
}

POLICY = {
    "agent": "worklist",
    "allow": [{"observe": ["read"]}, {"record": ["markWorked"]}],
    "gates": {
        "markWorked": {
            "precondition": {
                "checks": ["dispositionIsDeclared"],
                "resolvers": "role:supervisor",
            }
        }
    },
}


def _run(
    data: dict[str, Any],
    *,
    prior_refusal: bool = False,
    with_history: bool = True,
    registry_doc: dict[str, Any] | None = None,
    policy_doc: dict[str, Any] | None = None,
) -> Any:
    reg = load_registry(registry_doc or REGISTRY)
    audit = InMemoryAuditSink()
    policy = load_policy(policy_doc or POLICY, reg, schema=load_schema())
    engine = DefaultGateEngine(
        reg, history=AuditDecisionHistory(audit) if with_history else None
    )
    common: dict[str, Any] = {
        "registry": reg, "audit": audit, "policy": policy, "gates": engine,
        "outbox": InMemoryOutboxStore(audit=audit),
        "connectors": Connectors({"in_memory": InMemoryConnector()}),
    }
    if prior_refusal:
        # Something the policy does not allow: refused earlier in the same run,
        # which is exactly the fact rule 3 reads.
        refused = enforce(
            RawCall(resource="Supplier", action="updateBankAccount", data={}),
            ACTOR, RUN, **common,
        )
        assert refused.decision is Decision.DENY
    return enforce(
        RawCall(resource="Document", action="markWorked", data=data), ACTOR, RUN, **common
    )


# --- N1: no disposition ⇒ hold, nothing closed -----------------------------
def test_a_closure_with_no_disposition_holds() -> None:
    result = _run({"documentId": "D-1"})
    assert result.decision is Decision.HOLD
    assert result.reason_code == DISPOSITION_REQUIRED
    assert result.retry_class is not None and result.retry_class.value == "retryable"


def test_a_disposition_outside_the_vocabulary_holds() -> None:
    result = _run({"documentId": "D-1", "disposition": "handled-somehow"})
    assert result.decision is Decision.HOLD
    assert result.reason_code == DISPOSITION_REQUIRED


# --- N2: claiming completion after a refusal in the run ⇒ hold -------------
def test_claiming_completion_after_a_refusal_holds_and_names_it() -> None:
    result = _run({"documentId": "D-1", "disposition": "resolved"}, prior_refusal=True)
    assert result.decision is Decision.HOLD
    assert result.reason_code == CLOSED_WITHOUT_THE_WORK
    assert result.retry_class is not None and result.retry_class.value == "escalate"
    evidence = [g.evidence for g in result.gates if g.evidence]
    assert evidence and "Supplier.updateBankAccount" in evidence[0]["refusedInRun"]


def test_claiming_completion_with_a_clean_run_passes() -> None:
    result = _run({"documentId": "D-1", "disposition": "resolved"})
    assert result.decision is Decision.ALLOW


# --- N3: the honest exit stays open ---------------------------------------
def test_an_honest_disposition_passes_even_after_a_refusal() -> None:
    """The row that makes this a control rather than an outage.

    The actor was refused, and it may still close the item — as *escalated*. If
    this held too, the only way to satisfy the gate would be to do nothing, and
    the queue would fill with rows nobody can tell from a crash.
    """
    result = _run({"documentId": "D-1", "disposition": "escalated"}, prior_refusal=True)
    assert result.decision is Decision.ALLOW


# --- N4: unverifiable ⇒ fail closed, never a pass -------------------------
def test_a_completion_claim_fails_closed_with_no_history() -> None:
    result = _run({"documentId": "D-1", "disposition": "resolved"}, with_history=False)
    assert result.decision is Decision.DENY  # §10 fail-closed, not a silent allow


def test_the_check_on_an_action_with_no_closure_refuses_to_load() -> None:
    """Stronger than failing closed at runtime: §13.20 is an ERROR, and a policy
    with lint errors does not start. The runtime fail-closed path exists for a
    deployment that loaded without linting, and is exercised by the no-history
    case above."""
    reg = dict(REGISTRY)
    reg["resources"] = {
        "Document": {"actions": {"read": {"kind": "observe"},
                                 "markWorked": {"kind": "record"}}},
        "Supplier": REGISTRY["resources"]["Supplier"],  # type: ignore[index]
    }
    with pytest.raises(PolicyError):
        _run({"documentId": "D-1", "disposition": "resolved"}, registry_doc=reg)


def test_an_honest_disposition_does_not_need_history_at_all() -> None:
    """Rule 4 short-circuits before rule 3, so the cheap path stays cheap — and a
    deployment with no history wired can still let an actor be honest."""
    result = _run({"documentId": "D-1", "disposition": "referred"}, with_history=False)
    assert result.decision is Decision.ALLOW


# --- the boundary: only this gateway's own traffic -------------------------
def test_a_refusal_in_another_run_is_invisible() -> None:
    reg = load_registry(REGISTRY)
    audit = InMemoryAuditSink()
    policy = load_policy(POLICY, reg, schema=load_schema())
    common: dict[str, Any] = {
        "registry": reg, "audit": audit, "policy": policy,
        "gates": DefaultGateEngine(reg, history=AuditDecisionHistory(audit)),
        "outbox": InMemoryOutboxStore(audit=audit),
        "connectors": Connectors({"in_memory": InMemoryConnector()}),
    }
    other = Session(id="s2", correlation_id="run-2")
    enforce(RawCall(resource="Supplier", action="updateBankAccount", data={}),
            ACTOR, other, **common)
    # the refusal happened, in a different run: this closure is not that story
    result = enforce(
        RawCall(resource="Document", action="markWorked",
                data={"documentId": "D-1", "disposition": "resolved"}),
        ACTOR, RUN, **common,
    )
    assert result.decision is Decision.ALLOW


def test_another_actors_refusal_is_invisible() -> None:
    reg = load_registry(REGISTRY)
    audit = InMemoryAuditSink()
    policy = load_policy(POLICY, reg, schema=load_schema())
    common: dict[str, Any] = {
        "registry": reg, "audit": audit, "policy": policy,
        "gates": DefaultGateEngine(reg, history=AuditDecisionHistory(audit)),
        "outbox": InMemoryOutboxStore(audit=audit),
        "connectors": Connectors({"in_memory": InMemoryConnector()}),
    }
    enforce(RawCall(resource="Supplier", action="updateBankAccount", data={}),
            Actor(id="someone-else"), RUN, **common)
    result = enforce(
        RawCall(resource="Document", action="markWorked",
                data={"documentId": "D-1", "disposition": "resolved"}),
        ACTOR, RUN, **common,
    )
    assert result.decision is Decision.ALLOW


# --- the declaration itself ------------------------------------------------
def test_claims_completion_is_read_from_the_declared_field() -> None:
    reg = load_registry(REGISTRY)
    adef = reg.file.resources["Document"].actions["markWorked"]
    assert adef.closure is not None
    assert adef.closure.dispositionField == "disposition"
    assert adef.disposition_vocabulary() == ("resolved", "escalated", "referred", "duplicate")
    # a strict subset: there is always an honest way out
    assert set(adef.closure.claimsCompletion) < set(adef.disposition_vocabulary())


def test_an_undeclared_disposition_field_yields_no_vocabulary() -> None:
    reg = load_registry({"resources": {"Item": {"actions": {"close": {
        "kind": "record", "closure": {"dispositionField": "outcome"},
    }}}}})
    # the field is not in `data`, so the check is unsatisfiable rather than
    # permissive — nothing can be closed until the registry is fixed
    assert reg.file.resources["Item"].actions["close"].disposition_vocabulary() == ()


# --- §13.20, both directions ----------------------------------------------
def test_lint_refuses_the_check_on_an_action_with_no_closure() -> None:
    reg = load_registry({
        "resources": {"Document": {"actions": {"markWorked": {"kind": "record"}}}},
        "preconditionChecks": [{"name": "dispositionIsDeclared", "holdCapable": True,
                                "reasonCodes": {DISPOSITION_REQUIRED: "retryable"}}],
    })
    report = lint(Policy.model_validate(POLICY | {"allow": [{"record": ["markWorked"]}]}), reg)
    rule = [f for f in report.findings if f.code == "13.20"]
    assert rule and rule[0].severity is Severity.ERROR


def test_lint_warns_about_a_closure_nothing_enforces() -> None:
    reg = load_registry(REGISTRY)
    unenforced = {"agent": "worklist", "allow": [{"record": ["markWorked"]}]}
    report = lint(Policy.model_validate(unenforced), reg)
    rule = [f for f in report.findings if f.code == "13.20"]
    assert rule and rule[0].severity is Severity.WARN


# --- §13.19, promoted with §? ----------------------------------------
def test_lint_warns_on_a_per_item_threshold_with_no_aggregate() -> None:
    reg = load_registry({"resources": {"Payment": {"actions": {
        "pay": {"kind": "effect", "data": {"amount": {"type": "number"}}},
    }}}})
    doc = {"agent": "pay", "allow": [{"effect": ["pay"]}],
           "gates": {"pay": {"valueLimit": {"field": "data.amount", "max": 1000}}}}
    report = lint(Policy.model_validate(doc), reg)
    rule = [f for f in report.findings if f.code == "13.19"]
    assert rule and rule[0].severity is Severity.WARN


def test_lint_is_silent_when_an_aggregate_is_declared() -> None:
    reg = load_registry({"resources": {"Payment": {"actions": {
        "pay": {"kind": "effect", "data": {"amount": {"type": "number"}}},
    }}}})
    doc = {"agent": "pay", "allow": [{"effect": ["pay"]}],
           "gates": {"pay": {
               "valueLimit": {"field": "data.amount", "max": 1000},
               "spendLimit": "5000/day",
           }}}
    report = lint(Policy.model_validate(doc), reg)
    assert [f for f in report.findings if f.code == "13.19"] == []


# --- the standard check cannot be overridden -------------------------------
def test_a_local_implementation_does_not_replace_the_standard_check() -> None:
    """The name is reserved: a registered check of the same name would silently
    redefine a documented guarantee, so the standard one wins."""
    reg = load_registry(REGISTRY)
    engine = DefaultGateEngine(
        reg, preconditions={"dispositionIsDeclared": lambda _ctx: True},
    )
    assert engine.preconditions["dispositionIsDeclared"].__name__ == "disposition_is_declared"


def test_hold_reason_codes_are_declared_with_their_retry_classes() -> None:
    reg = load_registry(REGISTRY)
    decl = reg.file.precondition_decls["dispositionIsDeclared"]
    assert decl.holdCapable is True
    assert set(decl.reasonCodes) == {DISPOSITION_REQUIRED, CLOSED_WITHOUT_THE_WORK}


@pytest.mark.parametrize("disposition", ["escalated", "referred", "duplicate"])
def test_every_non_completion_disposition_is_a_way_out(disposition: str) -> None:
    assert _run({"documentId": "D-1", "disposition": disposition},
                prior_refusal=True).decision is Decision.ALLOW


def test_the_check_result_is_a_hold_not_a_fail() -> None:
    """A hold, not a deny: the actor is being asked a question it can answer, and
    the resolver contract is what a human uses to release it."""
    result = _run({"documentId": "D-1"})
    holds = [g for g in result.gates if g.outcome is Outcome.HOLD]
    assert holds and holds[0].code == DISPOSITION_REQUIRED
