"""v0.6 reason codes + retry classes — CS-029 (RFC §11).

Every deny/hold carries a machine-readable code with a declared retry class:
``retryable`` (fix the intent, resubmit) | ``terminal`` (stop) | ``escalate``
(surface to a human on the agent's side). Check codes take their class from the
registry declaration; built-ins carry the normative defaults; everything
undeclared is ``terminal``. Classification is pure table logic
(``stonefold_core.reasons``) — no new nondeterminism enters the pipeline.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import ValidationError

from stonefold_core import (
    Actor,
    Decision,
    InMemoryAuditSink,
    RawCall,
    RequestEnv,
    RetryClass,
    Session,
    enforce,
    load_policy,
    load_registry,
)
from stonefold_core.linter import lint
from stonefold_core.reasons import classify, gate_class, rule_class
from stonefold_gates.base import check_fail, check_hold
from stonefold_gates.engine import DefaultGateEngine
from tests.conftest import full_registry, load_schema

HOLD_CHECK = "matchesOpenPurchaseOrder"  # declared holdCapable + reasonCodes


def _run(
    gates: dict[str, Any],
    *,
    data: dict[str, Any] | None = None,
    preconditions: dict[str, Any] | None = None,
    action: tuple[str, str] = ("Payment", "pay"),
    resolvers: bool = True,
) -> Any:
    reg = full_registry()
    doc = {
        "agent": "t",
        "allow": [{"effect": [action[1]]}],
        "gates": {action[1]: gates},
    }
    policy = load_policy(doc, reg, schema=load_schema())
    engine = DefaultGateEngine(
        reg,
        preconditions=preconditions or {},
        default_resolver_role="role:ops" if resolvers else None,
    )
    return enforce(
        RawCall(resource=action[0], action=action[1], data=data or {}),
        Actor(id="alice"),
        Session(id="s1"),
        registry=reg,
        audit=InMemoryAuditSink(),
        policy=policy,
        gates=engine,
        env=RequestEnv(),
    )


# --- built-in gate classes (the normative table) ---------------------------


def test_value_limit_refusal_is_retryable() -> None:
    result = _run(
        {"valueLimit": {"field": "data.amount", "max": 100}}, data={"amount": 500}
    )
    assert result.decision is Decision.DENY
    assert result.rule == "gate:valueLimit"
    assert result.reason_code == "gate:valueLimit"  # no finer code declared
    assert result.retry_class is RetryClass.RETRYABLE


def test_allowlist_refusal_is_terminal() -> None:
    result = _run(
        {"allowlist": {"field": "data.country", "values": ["NL", "DE"]}},
        data={"country": "XX"},
    )
    assert result.decision is Decision.DENY
    assert result.retry_class is RetryClass.TERMINAL


def test_unknown_action_is_terminal() -> None:
    reg = full_registry()
    result = enforce(
        RawCall(resource="NoSuchThing", action="zap", data={}),
        Actor(id="alice"),
        Session(id="s1"),
        registry=reg,
        audit=InMemoryAuditSink(),
    )
    assert result.decision is Decision.DENY
    assert result.reason_code == "unknown-action"
    assert result.retry_class is RetryClass.TERMINAL


# --- check-declared classes -------------------------------------------------


def test_check_fail_code_takes_declared_class() -> None:
    result = _run(
        {"precondition": {"checks": [HOLD_CHECK]}},
        preconditions={HOLD_CHECK: lambda gctx: check_fail("amount-outside-tolerance")},
    )
    assert result.decision is Decision.DENY
    assert result.reason_code == "amount-outside-tolerance"
    assert result.retry_class is RetryClass.RETRYABLE  # declared in the registry


def test_check_hold_code_takes_declared_class() -> None:
    result = _run(
        {"precondition": {"checks": [HOLD_CHECK], "resolvers": "role:ap-clerk"}},
        preconditions={HOLD_CHECK: lambda gctx: check_hold("multiple-candidates")},
    )
    assert result.decision is Decision.HOLD
    assert result.reason_code == "multiple-candidates"
    assert result.retry_class is RetryClass.ESCALATE  # declared in the registry


def test_undeclared_code_defaults_terminal() -> None:
    result = _run(
        {"precondition": {"checks": [HOLD_CHECK]}},
        preconditions={HOLD_CHECK: lambda gctx: check_fail("some-novel-code")},
    )
    assert result.decision is Decision.DENY
    assert result.reason_code == "some-novel-code"
    assert result.retry_class is RetryClass.TERMINAL


def test_approval_hold_carries_no_class() -> None:
    # A held-for-approval intent's move is to WAIT — none of the three classes.
    result = _run({"requireApproval": {"approvers": "role:manager"}})
    assert result.decision is Decision.HOLD
    assert result.retry_class is None


def test_hold_from_undeclared_check_is_an_implementation_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # CS-026 rule 3: hold capability is a registry declaration. A bare-name
    # (two-valued) check that returns hold resolves fail-closed, loudly.
    with caplog.at_level(logging.ERROR, logger="stonefold.gates"):
        result = _run(
            {"precondition": {"checks": ["payeeCoolingOffElapsed"]}},
            preconditions={
                "payeeCoolingOffElapsed": lambda gctx: check_hold("no-open-match")
            },
        )
    assert result.decision is Decision.DENY
    assert any("not declared holdCapable" in r.message for r in caplog.records)


# --- registry declarations ---------------------------------------------------


def test_hold_capable_without_reason_codes_refuses_to_load() -> None:
    # Rule 18's registry half: every hold would be code-less and resolve fail.
    with pytest.raises(ValidationError, match="holdCapable without reasonCodes"):
        load_registry(
            {
                "resources": {"X": {"actions": {"go": {"kind": "effect"}}}},
                "preconditionChecks": [{"name": "badCheck", "holdCapable": True}],
            }
        )


def test_linter_warns_on_hold_capable_check_without_resolvers() -> None:
    # Rule 18's policy half (§13.18): warn, naming the deployment fallback.
    reg = full_registry()
    doc = {
        "agent": "t",
        "allow": [{"effect": ["pay"]}],
        "gates": {"pay": {"precondition": {"checks": [HOLD_CHECK]}}},
    }
    policy = load_policy(doc, reg, schema=load_schema())
    report = lint(policy.policy, reg)
    warnings = [f for f in report.warnings if f.code == "13.18"]
    assert warnings and "default resolver role" in warnings[0].message

    with_resolvers = {
        **doc,
        "gates": {"pay": {"precondition": {"checks": [HOLD_CHECK], "resolvers": "role:x"}}},
    }
    report2 = lint(load_policy(with_resolvers, reg, schema=load_schema()).policy, reg)
    assert not [f for f in report2.warnings if f.code == "13.18"]


# --- the classification tables (pure) ----------------------------------------


def test_classification_tables() -> None:
    assert gate_class("rate") is RetryClass.RETRYABLE
    assert gate_class("requireExplanation") is RetryClass.RETRYABLE
    assert gate_class("allowlist") is RetryClass.TERMINAL
    assert rule_class("stale-decision") is RetryClass.RETRYABLE
    assert rule_class("stale-guard:denylist") is RetryClass.RETRYABLE
    assert rule_class("expired-hold:precondition") is RetryClass.ESCALATE
    assert rule_class("hold-unresolvable") is RetryClass.ESCALATE
    assert rule_class("kill:k-1") is RetryClass.TERMINAL
    assert rule_class("outbox-unavailable") is RetryClass.TERMINAL  # undeclared default
    assert classify(Decision.ALLOW, "allow:pay", ()) == ("", None)


def test_audit_record_carries_code_and_class() -> None:
    reg = full_registry()
    audit = InMemoryAuditSink()
    doc = {
        "agent": "t",
        "allow": [{"effect": ["pay"]}],
        "gates": {"pay": {"valueLimit": {"field": "data.amount", "max": 100}}},
    }
    policy = load_policy(doc, reg, schema=load_schema())
    enforce(
        RawCall(resource="Payment", action="pay", data={"amount": 500}),
        Actor(id="alice"),
        Session(id="s1"),
        registry=reg,
        audit=audit,
        policy=policy,
        gates=DefaultGateEngine(reg),
        env=RequestEnv(),
    )
    record = audit.records[-1]
    assert record.reasonCode == "gate:valueLimit"
    assert record.retryClass is RetryClass.RETRYABLE
