"""M6 — failure mode (RFC §10, design §12) and acceptance **F3**.

A dependency failure (registry, scope resolver, contentCheck hook, kill store, or
the **outbox/audit DB**) must never bubble into an implicit allow (invariant 7).
The branch is taken from ``failureMode``: ``closed`` (default) denies/halts;
``open`` allows for low-stakes scopes — with one floor: an **irreversible effect
always fails closed**. F3 pins the outbox/audit-DB-down case: fail closed, and
audit best-effort to the fallback sink.
"""

from __future__ import annotations

from typing import Any

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
from stonefold_core.audit import FallbackAuditSink
from stonefold_core.enums import Kind, Reversibility
from stonefold_core.failure import Ok, Unavailable, guard, should_fail_closed
from stonefold_core.models import AuditRecord, Attributes, ResolvedAction
from stonefold_core.policy import FailureMode
from stonefold_connectors import InMemoryConnector
from stonefold_gates.engine import DefaultGateEngine
from stonefold_store import InMemoryOutboxStore
from tests.conftest import full_registry, load_schema


# --- a connector / outbox / sink that fail, to inject dependency outages ---
class _BrokenConnector:
    def execute(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError("connector down")

    def dispatch(self, *a: Any, **k: Any) -> Any:  # pragma: no cover - unused here
        raise RuntimeError("connector down")

    def fetch_target(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError("connector down")

    def cancel(self, handle: str) -> None:  # pragma: no cover - unused here
        raise RuntimeError("connector down")


class _BrokenOutbox(InMemoryOutboxStore):
    def stage(self, **k: Any) -> Any:
        raise RuntimeError("outbox DB unavailable")


class _BrokenSink:
    def write(self, record: AuditRecord) -> None:
        raise RuntimeError("audit DB unavailable")


def _enforce(
    doc: dict[str, Any],
    *,
    resource: str,
    action: str,
    audit: Any = None,
    connectors: Any = None,
    outbox: Any = None,
    data: dict[str, Any] | None = None,
) -> Any:
    reg = full_registry()
    policy = load_policy(doc, reg, schema=load_schema())
    the_audit = audit or InMemoryAuditSink()
    return enforce(
        RawCall(resource=resource, action=action, data=data or {}),
        Actor(id="alice"),
        Session(id="s1", correlation_id="run-F3"),
        registry=reg, audit=the_audit, policy=policy,
        gates=DefaultGateEngine(reg),
        connectors=connectors, outbox=outbox,
    )


# --- the pure resolution ---------------------------------------------------
def _effect(reversibility: Reversibility) -> ResolvedAction:
    return ResolvedAction(
        kind=Kind.EFFECT, resource="X", action="do", data={},
        attrs=Attributes(reversibility=reversibility), connector="in_memory",
    )


def test_should_fail_closed_resolution() -> None:
    irreversible = _effect(Reversibility.IRREVERSIBLE)
    reversible = _effect(Reversibility.REVERSIBLE)
    # closed ⇒ always fail closed
    assert should_fail_closed(reversible, FailureMode.CLOSED) is True
    assert should_fail_closed(irreversible, FailureMode.CLOSED) is True
    # open ⇒ allow through, EXCEPT the irreversible floor
    assert should_fail_closed(reversible, FailureMode.OPEN) is False
    assert should_fail_closed(irreversible, FailureMode.OPEN) is True
    # an unresolved action under open is low-stakes (allow); under closed, deny
    assert should_fail_closed(None, FailureMode.OPEN) is False
    assert should_fail_closed(None, FailureMode.CLOSED) is True


def test_guard_captures_exceptions_as_unavailable() -> None:
    assert guard(lambda: 21 * 2, reason="r") == Ok(42)
    failed = guard(lambda: (_ for _ in ()).throw(RuntimeError("boom")), reason="dep-down")
    assert isinstance(failed, Unavailable) and failed.reason == "dep-down"


# --- failureMode closed vs open on a connector outage (observe) ------------
def test_connector_unavailable_closed_denies() -> None:
    doc = {"agent": "support", "defaults": {"failureMode": "closed"},
           "allow": [{"observe": ["read"]}]}
    result = _enforce(doc, resource="Customer", action="read",
                      connectors=Connectors({"sql": _BrokenConnector()}))
    assert result.decision is Decision.DENY
    assert result.rule == "connector-unavailable"


def test_connector_unavailable_open_allows() -> None:
    # RFC §10: open ⇒ a low-stakes dependency outage is allowed through.
    doc = {"agent": "support", "defaults": {"failureMode": "open"},
           "allow": [{"observe": ["read"]}]}
    result = _enforce(doc, resource="Customer", action="read",
                      connectors=Connectors({"sql": _BrokenConnector()}))
    assert result.decision is Decision.ALLOW
    assert result.output is None  # nothing came back, but the action was permitted


# --- F3: the outbox/audit DB is the durability+evidence layer; always closed
def test_f3_outbox_db_down_fails_closed_and_is_audited() -> None:
    audit = InMemoryAuditSink()
    doc = {"agent": "support", "allow": [{"effect": ["sendEmail"]}]}
    result = _enforce(doc, resource="Email", action="sendEmail",
                      data={"to": "x@acme.example"}, audit=audit,
                      connectors=Connectors({"email": InMemoryConnector()}),
                      outbox=_BrokenOutbox(audit=audit))
    assert result.decision is Decision.DENY
    assert result.rule == "outbox-unavailable"
    # the refusal is still recorded (the audit record carries decision/outcome,
    # not the rule), and nothing was sent.
    denials = [r for r in audit.records
               if r.decision is Decision.DENY and r.resource == "Email"]
    assert len(denials) == 1 and denials[0].outcome == "not_executed"


def test_f3_outbox_down_even_with_open_fails_closed() -> None:
    # losing the staging substrate is not a "low-stakes" outage: open does not
    # turn an unstageable effect into an allow. (Uses a *compensable* effect — an
    # irreversible one under failureMode: open is itself a §13.5 linter error.)
    doc = {"agent": "support", "defaults": {"failureMode": "open"},
           "allow": [{"effect": ["pay"]}]}
    result = _enforce(doc, resource="Payment", action="pay",
                      data={"amount": 1},
                      connectors=Connectors({"sql": InMemoryConnector()}),
                      outbox=_BrokenOutbox())
    assert result.decision is Decision.DENY
    assert result.rule == "outbox-unavailable"


def test_f3_audit_db_down_falls_back_best_effort() -> None:
    # The primary (durable) sink is down; the FallbackAuditSink keeps the record
    # so a refusal is never *also* unaudited.
    fallback = InMemoryAuditSink()
    sink = FallbackAuditSink(primary=_BrokenSink(), fallback=fallback)
    doc = {"agent": "pay", "allow": [{"effect": ["pay"]}]}
    result = _enforce(doc, resource="Payment", action="refund",  # not allowed ⇒ DENY
                      data={"amount": 1}, audit=sink)
    assert result.decision is Decision.DENY
    assert sink.failures == 1
    assert len(fallback.records) == 1
    assert fallback.records[0].decision is Decision.DENY
