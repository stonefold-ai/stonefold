"""The advisory profile's two keys, and its visibility.

A policy may declare advisory enforcement; only a deployment that permits it can
load that policy. Both keys are required, and the mode is never quiet: it is a
lint warning, a startup banner, and a stats surface.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from stonefold_core import (
    Actor,
    Connectors,
    Decision,
    EnforcementMode,
    EnforcementNotPermittedError,
    InMemoryAuditSink,
    RawCall,
    Session,
    load_policy,
    validate_only,
)
from stonefold_connectors import InMemoryConnector
from stonefold_gates.engine import DefaultGateEngine
from stonefold_gateway.admin_api import advisory_stats
from stonefold_gateway.main import ADVISORY_BANNER, create_app
from stonefold_gateway.transport import Gateway
from stonefold_store import InMemoryOutboxStore
from tests.conftest import full_registry, load_schema

_ADVISORY_DOC: dict[str, Any] = {
    "agent": "support",
    "defaults": {"enforcement": "advisory"},
    "allow": [{"observe": ["read"]}],
}
_ENFORCING_DOC: dict[str, Any] = {
    "agent": "support",
    "allow": [{"observe": ["read"]}],
}


# --- key 1: the policy declares -----------------------------------------
def test_a_policy_declares_the_mode() -> None:
    compiled = load_policy(
        _ADVISORY_DOC, full_registry(), schema=load_schema(), advisory_permitted=True
    )

    assert compiled.policy.effective_enforcement is EnforcementMode.ADVISORY
    assert compiled.policy.is_advisory


def test_silence_means_enforcing() -> None:
    """Every policy written before this profile existed keeps its meaning."""
    compiled = load_policy(_ENFORCING_DOC, full_registry(), schema=load_schema())

    assert compiled.policy.effective_enforcement is EnforcementMode.ENFORCED
    assert not compiled.policy.is_advisory


# --- key 2: the deployment permits --------------------------------------
def test_a_policy_cannot_turn_enforcement_off_by_itself() -> None:
    """The whole point of two keys: a file in a directory is not authority to
    stop enforcing."""
    with pytest.raises(EnforcementNotPermittedError) as exc:
        load_policy(_ADVISORY_DOC, full_registry(), schema=load_schema())

    assert "advisory" in str(exc.value)
    assert "support" in str(exc.value)


def test_permission_is_not_needed_to_enforce() -> None:
    """A deployment that permits advisory does not become advisory; only a
    policy that asks for it does."""
    compiled = load_policy(
        _ENFORCING_DOC, full_registry(), schema=load_schema(), advisory_permitted=True
    )

    assert compiled.policy.effective_enforcement is EnforcementMode.ENFORCED


def test_the_refusal_happens_at_load_not_per_request() -> None:
    """A running gateway whose mode nobody agreed on is the state neither
    answer is safe in."""
    with pytest.raises(EnforcementNotPermittedError):
        load_policy(_ADVISORY_DOC, full_registry(), schema=load_schema())


# --- the mode is never quiet ---------------------------------------------
def test_the_linter_says_nothing_below_will_be_enforced() -> None:
    report = validate_only(_ADVISORY_DOC, full_registry(), schema=load_schema())

    warnings = [w for w in report.warnings if w.code == "advisory"]
    assert warnings, "an advisory policy must warn"
    assert "none is enforced" in warnings[0].message
    # Advisory is a legitimate posture — a warning, never an error.
    assert not report.has_errors


def test_an_enforcing_policy_does_not_warn() -> None:
    report = validate_only(_ENFORCING_DOC, full_registry(), schema=load_schema())

    assert [w for w in report.warnings if w.code == "advisory"] == []


def _gateway(doc: dict[str, Any], audit: InMemoryAuditSink) -> Gateway:
    reg = full_registry()
    policy = load_policy(
        doc, reg, schema=load_schema(), advisory_permitted=True
    )
    return Gateway(
        registry=reg,
        audit=audit,
        policy=policy,
        gates=DefaultGateEngine(reg),
        outbox=InMemoryOutboxStore(audit=audit),
        connectors=Connectors(
            {
                "in_memory": InMemoryConnector(),
                "email": InMemoryConnector(),
                "sql": InMemoryConnector(),
            }
        ),
    )


def test_the_gateway_carries_the_declared_mode() -> None:
    audit = InMemoryAuditSink()

    assert _gateway(_ADVISORY_DOC, audit).enforcement is EnforcementMode.ADVISORY
    assert _gateway(_ENFORCING_DOC, audit).enforcement is EnforcementMode.ENFORCED


def test_startup_says_enforcement_is_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An operator who does not know the gateway is advisory has a control they
    believe in and do not have."""
    audit = InMemoryAuditSink()
    with caplog.at_level(logging.WARNING, logger="stonefold.gateway"):
        create_app(_gateway(_ADVISORY_DOC, audit))

    assert ADVISORY_BANNER in caplog.text
    assert "ENFORCEMENT IS OFF" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="stonefold.gateway"):
        create_app(_gateway(_ENFORCING_DOC, audit))
    assert "ENFORCEMENT IS OFF" not in caplog.text


# --- the declared mode actually runs -------------------------------------
def test_a_declared_advisory_policy_advises_through_the_transport() -> None:
    """The keys are not decoration: the mode reaches ``enforce``."""
    audit = InMemoryAuditSink()
    gateway = _gateway(_ADVISORY_DOC, audit)

    result = gateway.submit(
        resource="Email",
        action="sendEmail",
        data={},
        actor=Actor(id="alice"),
        session=Session(id="s1", correlation_id="corr-1"),
    )

    assert result.decision is Decision.ALLOW
    record = audit.records[-1]
    assert record.enforcement is EnforcementMode.ADVISORY
    assert record.advised is not None
    assert record.advised.decision is Decision.DENY


# --- the live surface: counts, not verdicts ------------------------------
def test_the_stats_surface_counts_and_stops_there() -> None:
    audit = InMemoryAuditSink()
    gateway = _gateway(_ADVISORY_DOC, audit)
    for _ in range(3):
        gateway.submit(
            resource="Email", action="sendEmail", data={},
            actor=Actor(id="alice"),
            session=Session(id="s1", correlation_id="corr-1"),
        )
    gateway.submit(
        resource="Customer", action="read", data={},
        actor=Actor(id="alice"),
        session=Session(id="s1", correlation_id="corr-1"),
    )

    stats = advisory_stats(audit.all_records())

    assert stats["mode"] == "advisory"
    assert stats["wouldDeny"] == 3
    assert stats["wouldHold"] == 0
    assert stats["advisoryRecords"] == 4
    assert stats["judged"] == 4
    # Counts only: no resource, no action, no rule, nothing to act on today.
    assert all(
        key
        in {
            "mode", "judged", "unjudged", "wouldDeny", "wouldHold",
            "enforcedRecords", "advisoryRecords",
        }
        for key in stats
    )


def test_mode_is_the_declared_one_not_the_traffic_derived_one() -> None:
    """An advisory deployment is advisory before its first request. Deriving
    the mode from records would report 'enforced' on a quiet advisory gateway —
    exactly the confusion the banner exists to prevent."""
    assert (
        advisory_stats([], declared=EnforcementMode.ADVISORY)["mode"] == "advisory"
    )
    assert (
        advisory_stats([], declared=EnforcementMode.ENFORCED)["mode"] == "enforced"
    )
    # Fallback for report tooling over an exported audit, with no gateway.
    assert advisory_stats([])["mode"] == "enforced"


def test_mixed_records_are_visible_rather_than_averaged() -> None:
    """No figure may be averaged across the two modes, so the surface says
    plainly that both are present."""
    audit = InMemoryAuditSink()
    _gateway(_ADVISORY_DOC, audit).submit(
        resource="Email", action="sendEmail", data={},
        actor=Actor(id="alice"), session=Session(id="s1"),
    )
    _gateway(_ENFORCING_DOC, audit).submit(
        resource="Email", action="sendEmail", data={},
        actor=Actor(id="alice"), session=Session(id="s2"),
    )

    stats = advisory_stats(audit.all_records())

    assert stats["advisoryRecords"] == 1
    assert stats["enforcedRecords"] == 1
