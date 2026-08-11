"""Advisory mode — the verdict is computed and recorded, the action goes through.

An advisory deployment sits in the real traffic path and refuses nothing, so its
audit holds the counterfactual: what an enforcing deployment would have done to
the same traffic. These tests pin the properties that claim rests on.

The load-bearing one is ``test_advisory_and_enforced_verdicts_are_identical``:
advisory and enforcing runs of the same fixture must reach the same verdict, or
"would have held" is not a measurement of anything.
"""

from __future__ import annotations

from typing import Any

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
    enforce_batch,
    load_policy,
)
from stonefold_core.enums import Coverage, EnforcementMode
from stonefold_core.pipeline import ADVISORY_RULE
from stonefold_connectors import InMemoryConnector
from stonefold_gates.engine import DefaultGateEngine
from stonefold_store import InMemoryOutboxStore
from stonefold_store.kill_memory import InMemoryKillStore
from tests.conftest import full_registry, load_schema


def _run(
    doc: dict[str, Any],
    *,
    resource: str,
    action: str,
    enforcement: EnforcementMode,
    kill: Any = None,
    data: dict[str, Any] | None = None,
) -> Any:
    reg = full_registry()
    policy = load_policy(doc, reg, schema=load_schema())
    audit = InMemoryAuditSink()
    outbox = InMemoryOutboxStore(audit=audit)
    connectors = Connectors(
        {
            "in_memory": InMemoryConnector(),
            "email": InMemoryConnector(),
            "sql": InMemoryConnector(),
        }
    )
    result = enforce(
        RawCall(resource=resource, action=action, data=data or {}),
        Actor(id="alice"),
        Session(id="s1", correlation_id="corr-1"),
        registry=reg,
        audit=audit,
        policy=policy,
        gates=DefaultGateEngine(reg),
        outbox=outbox,
        connectors=connectors,
        kill=kill,
        enforcement=enforcement,
    )
    return result, audit, outbox


# The policy allows a read and says nothing about sending email, so the effect
# is refused by default-deny — the commonest refusal there is.
_DENY_DOC = {"agent": "support", "allow": [{"observe": ["read"]}]}
_ALLOW_DOC = {
    "agent": "support",
    "allow": [{"observe": ["read"]}, {"effect": ["sendEmail"]}],
}


# --- what the actor sees, and what the record keeps ----------------------
def test_advisory_lets_a_denied_action_through() -> None:
    result, audit, _ = _run(
        _DENY_DOC,
        resource="Email",
        action="sendEmail",
        enforcement=EnforcementMode.ADVISORY,
    )

    assert result.decision is Decision.ALLOW
    record = audit.records[-1]
    assert record.enforcement is EnforcementMode.ADVISORY
    assert record.advised is not None
    assert record.advised.decision is Decision.DENY
    # The record keeps the rule that decided (spec §11); the actor does not.
    assert record.rule == record.advised.rule
    assert result.rule == ADVISORY_RULE


def test_the_actor_is_not_told_it_was_advised() -> None:
    """An actor that learns nothing will stop it has learned something the
    measurement cannot survive: the traffic stops being ordinary traffic."""
    result, _, _ = _run(
        _DENY_DOC,
        resource="Email",
        action="sendEmail",
        enforcement=EnforcementMode.ADVISORY,
    )

    assert not hasattr(result, "advised")
    assert not hasattr(result, "enforcement")
    assert result.reason_code == ""
    assert result.rule == ADVISORY_RULE
    # The gate trace stays on the result as it does for any allow; nothing in it
    # names the mode.
    assert ADVISORY_RULE not in str(result.gates)


def test_an_allowed_action_carries_no_advice() -> None:
    """``advised`` marks divergence, not mode — ``enforcement`` says the mode."""
    result, audit, _ = _run(
        _ALLOW_DOC,
        resource="Email",
        action="sendEmail",
        enforcement=EnforcementMode.ADVISORY,
    )

    assert result.decision is Decision.ALLOW
    record = audit.records[-1]
    assert record.enforcement is EnforcementMode.ADVISORY
    assert record.advised is None
    assert record.coverage is Coverage.JUDGED


def test_enforced_records_say_so() -> None:
    _, audit, _ = _run(
        _DENY_DOC,
        resource="Email",
        action="sendEmail",
        enforcement=EnforcementMode.ENFORCED,
    )

    record = audit.records[-1]
    assert record.enforcement is EnforcementMode.ENFORCED
    assert record.advised is None
    assert record.decision is Decision.DENY


# --- the property the report rests on ------------------------------------
def test_advisory_and_enforced_verdicts_are_identical() -> None:
    """TCK A-8: same fixture, two modes, same verdict.

    Advisory changes what happens after the verdict, never the verdict.
    """
    cases = [
        (_DENY_DOC, "Email", "sendEmail"),
        (_ALLOW_DOC, "Email", "sendEmail"),
        (_ALLOW_DOC, "Customer", "read"),
    ]
    for doc, resource, action in cases:
        enforced_result, enforced_audit, _ = _run(
            doc, resource=resource, action=action,
            enforcement=EnforcementMode.ENFORCED,
        )
        _, advisory_audit, _ = _run(
            doc, resource=resource, action=action,
            enforcement=EnforcementMode.ADVISORY,
        )

        enforced_record = enforced_audit.records[-1]
        advisory_record = advisory_audit.records[-1]
        would_be = (
            advisory_record.advised.decision
            if advisory_record.advised is not None
            else advisory_record.decision
        )
        assert would_be is enforced_result.decision
        would_be_rule = (
            advisory_record.advised.rule
            if advisory_record.advised is not None
            else advisory_record.rule
        )
        assert would_be_rule == enforced_record.rule
        # The evidence behind the verdict survives the translation too.
        assert [g.gate for g in advisory_record.gates] == [
            g.gate for g in enforced_record.gates
        ]


# --- holds do not stage --------------------------------------------------
def test_an_advised_hold_stages_nothing() -> None:
    """Nobody is going to answer a hold in a deployment that does not stop
    anything, and the effect has already happened."""
    doc = {
        "agent": "support",
        "allow": [{"effect": ["sendEmail"]}],
        "gates": {
            "Email.sendEmail": {"requireApproval": {"approvers": "role:manager"}}
        },
    }

    enforced, enforced_audit, enforced_outbox = _run(
        doc, resource="Email", action="sendEmail",
        enforcement=EnforcementMode.ENFORCED,
    )
    assert enforced.decision is Decision.HOLD
    assert enforced_outbox.list_by_state(PendingState.PENDING_APPROVAL)

    result, audit, outbox = _run(
        doc, resource="Email", action="sendEmail",
        enforcement=EnforcementMode.ADVISORY,
    )

    assert result.decision is Decision.ALLOW
    # The effect stages and dispatches exactly as any allowed effect does
    # (invariant 4 is untouched); what is empty is the HUMAN queue.
    assert outbox.list_by_state(PendingState.PENDING_APPROVAL) == []
    assert audit.records[-1].advised is not None
    assert audit.records[-1].advised.decision is Decision.HOLD


# --- the operator's cord still works -------------------------------------
def test_a_kill_order_still_halts_under_advisory() -> None:
    """The single lever an advisory deployment promises to keep."""
    kill = InMemoryKillStore()
    kill.issue(KillScope.for_session("s1"), issued_by="operator")

    result, audit, _ = _run(
        _ALLOW_DOC,
        resource="Email",
        action="sendEmail",
        enforcement=EnforcementMode.ADVISORY,
        kill=kill,
    )

    assert result.decision is Decision.HALT
    record = audit.records[-1]
    assert record.decision is Decision.HALT
    # A halt that happened is not a halt that would have happened.
    assert record.advised is None
    assert record.enforcement is EnforcementMode.ADVISORY


# --- batches -------------------------------------------------------------
def _run_batch(doc: dict[str, Any], calls: list[RawCall], *,
               enforcement: EnforcementMode) -> Any:
    reg = full_registry()
    policy = load_policy(doc, reg, schema=load_schema())
    audit = InMemoryAuditSink()
    outbox = InMemoryOutboxStore(audit=audit)
    connectors = Connectors(
        {
            "in_memory": InMemoryConnector(),
            "email": InMemoryConnector(),
            "sql": InMemoryConnector(),
        }
    )
    result = enforce_batch(
        calls,
        Actor(id="alice"),
        Session(id="s1", correlation_id="corr-1"),
        registry=reg,
        audit=audit,
        policy=policy,
        gates=DefaultGateEngine(reg),
        outbox=outbox,
        connectors=connectors,
        enforcement=enforcement,
    )
    return result, audit


def test_an_advised_batch_commits_whole_and_records_what_refused_it() -> None:
    """TCK A-9. Atomicity is a property of the batch, so no single operation's
    record can carry it — every operation carries ``batchAdvice``."""
    calls = [
        RawCall(resource="Customer", action="read"),
        RawCall(resource="Email", action="sendEmail"),
    ]

    enforced, _ = _run_batch(
        _DENY_DOC, calls, enforcement=EnforcementMode.ENFORCED
    )
    assert enforced.decision is Decision.DENY
    assert enforced.failing_index == 1

    result, audit = _run_batch(
        _DENY_DOC, calls, enforcement=EnforcementMode.ADVISORY
    )

    assert result.decision is Decision.ALLOW
    assert result.failing_index is None
    records = audit.records[-2:]
    for record in records:
        assert record.enforcement is EnforcementMode.ADVISORY
        assert record.batchAdvice == {
            "wouldRefuse": True,
            "failingIndex": 1,
            "decision": "deny",
        }
    # Each operation still carries the verdict it earned itself.
    assert records[0].advised is None
    assert records[1].advised is not None
    assert records[1].advised.decision is Decision.DENY
