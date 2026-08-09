"""v0.6.1 CS-044 — a gate that names what it reads (RFC §7.6, registry docs/06 §5e).

Three failures, one primitive. A control reads content that ages; while the
content is current nobody looks at the control. So:

* a gate whose author forgot the freshness check answers confidently from last
  year's content, and no analysis over the policy can find it;
* a stale copy and an unreachable source refuse identically, so "the world says
  no" and "my copy is old" cannot be told apart;
* an estate that cannot report a state has no way to say *governed, but the guard
  is unavailable*, so the honest declaration denies every legitimate attempt.

The tests are grouped by which of those three each one closes, and the last group
is the one that matters most: the question a reviewer asks of a policy that is not
running.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
from stonefold_core.gating import RequestEnv
from stonefold_core.inspect import declared_sources_unread, gates_reading
from stonefold_core.linter import Severity, lint
from stonefold_core.policy import Policy
from stonefold_core.registry import load_registry
from stonefold_connectors import InMemoryConnector
from stonefold_gates.base import GateContext, check_pass
from stonefold_gates.engine import DefaultGateEngine
from stonefold_gates.reads import (
    SOURCE_STALE,
    SOURCE_UNAVAILABLE,
    SOURCE_UNDATED,
)
from stonefold_store import InMemoryOutboxStore
from tests.conftest import load_schema

ACTOR = Actor(id="clinician-agent")
RUN = Session(id="s1", correlation_id="run-1")
NOW = datetime(2026, 8, 9, 9, 0, tzinfo=timezone.utc)

REGISTRY: dict[str, Any] = {
    "resources": {
        "Result": {
            "actions": {
                # gated, and it declares what its check depends on
                "dismiss": {"kind": "record", "label": "Dismiss a result."},
                # the neighbour whose author forgot: same class, same risk, no read
                "route": {"kind": "record", "label": "Route a result to a clinician."},
            }
        }
    },
    "preconditionChecks": ["analyteIsNotCritical"],
    "sources": {
        "critical-analyte-list": {
            "kind": "ruleSet",
            "label": "Analytes requiring same-day clinician review",
        },
        "sanctions-list": {"kind": "ruleSet"},
    },
}

POLICY: dict[str, Any] = {
    "agent": "results",
    "allow": [{"record": ["dismiss", "route"]}],
    "gates": {
        "dismiss": {
            "precondition": {
                "checks": ["analyteIsNotCritical"],
                "reads": [{"source": "critical-analyte-list", "freshness": "30d"}],
                "onUnavailable": "hold",
                "resolvers": "role:clinician",
            }
        },
        "route": {"precondition": ["analyteIsNotCritical"]},
    },
}


class _Dated:
    """A source that reports its own age."""

    def __init__(self, as_of: datetime | None) -> None:
        self._as_of = as_of

    def as_of(self) -> datetime | None:
        return self._as_of


class _Broken:
    def as_of(self) -> datetime | None:
        raise RuntimeError("rule service unreachable")


def _run(
    *,
    sources: dict[str, Any] | None = None,
    policy_doc: dict[str, Any] | None = None,
    action: str = "dismiss",
    clock: datetime | None = NOW,
) -> Any:
    reg = load_registry(REGISTRY)
    audit = InMemoryAuditSink()
    return enforce(
        RawCall(resource="Result", action=action, data={"resultId": "R-1"}),
        ACTOR, RUN,
        registry=reg, audit=audit,
        env=RequestEnv(now=clock) if clock else RequestEnv(),
        policy=load_policy(policy_doc or POLICY, reg, schema=load_schema()),
        gates=DefaultGateEngine(
            reg,
            preconditions={"analyteIsNotCritical": lambda _c: check_pass()},
            sources=sources or {},
        ),
        outbox=InMemoryOutboxStore(audit=audit),
        connectors=Connectors({"in_memory": InMemoryConnector()}),
    )


# --- F-21: a stale source is distinguishable from an unreachable one -------
def test_a_fresh_source_lets_the_check_run() -> None:
    result = _run(sources={"critical-analyte-list": _Dated(NOW - timedelta(days=3))})
    assert result.decision is Decision.ALLOW


def test_a_stale_source_holds_and_says_how_stale() -> None:
    """Not a deny: the content was read, and 'this rule is past its review date,
    decide' is a question for a human (§7.6 rule 1)."""
    result = _run(sources={"critical-analyte-list": _Dated(NOW - timedelta(days=55))})
    assert result.decision is Decision.HOLD
    assert result.reason_code == SOURCE_STALE
    evidence = [g.evidence for g in result.gates if g.evidence][0]
    assert evidence["source"] == "critical-analyte-list"
    assert evidence["ageDays"] == 55.0 and evidence["maxAgeDays"] == 30.0


def test_an_unreachable_source_is_distinguishable_from_a_stale_one() -> None:
    """The whole of F-21: same action, same gate, two different reasons, two
    different codes. Previously both were one denial with one code."""
    stale = _run(sources={"critical-analyte-list": _Dated(NOW - timedelta(days=55))})
    broken = _run(sources={"critical-analyte-list": _Broken()})
    assert stale.reason_code == SOURCE_STALE
    assert broken.reason_code == SOURCE_UNAVAILABLE
    assert stale.reason_code != broken.reason_code


def test_undated_content_is_not_treated_as_fresh() -> None:
    result = _run(sources={"critical-analyte-list": _Dated(None)})
    assert result.decision is Decision.HOLD
    assert result.reason_code == SOURCE_UNDATED


def test_a_declared_source_with_no_adapter_is_unreadable_not_fresh() -> None:
    """A dependency the deployment never built must not read as satisfied."""
    result = _run(sources={})
    assert result.reason_code == SOURCE_UNAVAILABLE


def test_no_clock_means_the_age_is_unknown_not_acceptable() -> None:
    """Invariant 1: never invent a time. An unknowable age is an outage."""
    result = _run(sources={"critical-analyte-list": _Dated(NOW - timedelta(days=1))},
                  clock=None)
    assert result.reason_code == SOURCE_UNAVAILABLE


# --- F-16: governed, but the guard is unavailable --------------------------
def test_on_unavailable_hold_queues_for_a_human_instead_of_denying_forever() -> None:
    """F-16's case. An estate that cannot report the state declares this, and the
    action becomes a question rather than a permanent refusal or a mislabelled
    record."""
    result = _run(sources={"critical-analyte-list": _Broken()})
    assert result.decision is Decision.HOLD
    assert result.ticket, "the held action must be releasable by a human"


def test_the_default_for_an_unreadable_source_is_deny() -> None:
    """§7.6 rule 1: outages fail. If they held, every blip would become a human
    interruption and fail-closed would decay into a rubber-stamped queue."""
    doc = {
        "agent": "results",
        "allow": [{"record": ["dismiss"]}],
        "gates": {"dismiss": {"precondition": {
            "checks": ["analyteIsNotCritical"],
            "reads": [{"source": "critical-analyte-list", "freshness": "30d"}],
        }}},
    }
    result = _run(sources={"critical-analyte-list": _Broken()}, policy_doc=doc)
    assert result.decision is Decision.DENY
    assert result.reason_code == SOURCE_UNAVAILABLE


@pytest.mark.parametrize("disposition,expected", [
    ("hold", Decision.HOLD), ("deny", Decision.DENY), ("allow", Decision.ALLOW),
])
def test_on_stale_honours_the_declared_disposition(
    disposition: str, expected: Decision
) -> None:
    doc = {
        "agent": "results",
        "allow": [{"record": ["dismiss"]}],
        "gates": {"dismiss": {"precondition": {
            "checks": ["analyteIsNotCritical"],
            "reads": [{"source": "critical-analyte-list", "freshness": "30d",
                       "onStale": disposition}],
            "onUnavailable": "deny",
            "resolvers": "role:clinician",
        }}},
    }
    result = _run(sources={"critical-analyte-list": _Dated(NOW - timedelta(days=55))},
                  policy_doc=doc)
    assert result.decision is expected


def test_naming_a_source_without_a_freshness_requirement_only_checks_reachability() -> None:
    doc = {
        "agent": "results",
        "allow": [{"record": ["dismiss"]}],
        "gates": {"dismiss": {"precondition": {
            "checks": ["analyteIsNotCritical"],
            "reads": ["critical-analyte-list"],
            "onUnavailable": "deny",
        }}},
    }
    ancient = _run(sources={"critical-analyte-list": _Dated(NOW - timedelta(days=900))},
                   policy_doc=doc)
    assert ancient.decision is Decision.ALLOW  # no currency was demanded
    gone = _run(sources={}, policy_doc=doc)
    assert gone.decision is Decision.DENY      # reachability still is


# --- F-39: the question a reviewer asks of a policy that is not running ----
def test_the_policy_answers_which_gates_read_an_overdue_rule_set() -> None:
    """The finding, in one call. No adapters, no clock, no I/O."""
    reg = load_registry(REGISTRY)
    report = gates_reading(Policy.model_validate(POLICY), reg, "critical-analyte-list")

    assert report.declared is True
    assert [(r.resource, r.action, r.gate) for r in report.readers] == [
        ("Result", "dismiss", "precondition")
    ]
    assert report.readers[0].freshness == "30d"
    assert report.readers[0].on_stale == "hold"
    assert report.readers[0].on_unavailable == "hold"


def test_the_gate_whose_author_forgot_is_the_finding() -> None:
    """`Result.route` is the same class of action, gated, and reads nothing. It is
    invisible at runtime — it answers confidently from whatever the check happens
    to load — and visible only beside the neighbour that declared the dependency."""
    reg = load_registry(REGISTRY)
    report = gates_reading(Policy.model_validate(POLICY), reg, "critical-analyte-list")
    assert ("Result", "route", "precondition") in report.silent


def test_the_summary_reads_like_the_question() -> None:
    reg = load_registry(REGISTRY)
    text = gates_reading(Policy.model_validate(POLICY), reg, "critical-analyte-list").summary()
    assert "read by 1 gate" in text
    assert "Result.dismiss" in text
    assert "reads nothing" in text


def test_an_undeclared_source_is_reported_as_undeclared() -> None:
    reg = load_registry(REGISTRY)
    assert gates_reading(Policy.model_validate(POLICY), reg, "no-such-list").declared is False


def test_a_declared_source_nobody_reads_is_findable() -> None:
    """Usually a leftover; occasionally the interesting case, because somebody
    knew the dependency existed and no control uses it."""
    reg = load_registry(REGISTRY)
    assert declared_sources_unread(Policy.model_validate(POLICY), reg) == ("sanctions-list",)


# --- §13.22 ----------------------------------------------------------------
def test_lint_refuses_a_read_of_an_undeclared_source() -> None:
    reg = load_registry(REGISTRY)
    doc = {
        "agent": "results", "allow": [{"record": ["dismiss"]}],
        "gates": {"dismiss": {"precondition": {
            "checks": ["analyteIsNotCritical"],
            "reads": ["typo-analyte-list"],
            "onUnavailable": "deny",
        }}},
    }
    findings = [f for f in lint(Policy.model_validate(doc), reg).findings if f.code == "13.22"]
    assert findings and findings[0].severity is Severity.ERROR


def test_lint_warns_when_the_unavailable_disposition_is_left_implicit() -> None:
    reg = load_registry(REGISTRY)
    doc = {
        "agent": "results", "allow": [{"record": ["dismiss"]}],
        "gates": {"dismiss": {"precondition": {
            "checks": ["analyteIsNotCritical"],
            "reads": [{"source": "critical-analyte-list", "freshness": "30d"}],
        }}},
    }
    findings = [f for f in lint(Policy.model_validate(doc), reg).findings if f.code == "13.22"]
    assert findings and any("onUnavailable" in f.message for f in findings)


def test_lint_warns_about_accepting_stale_content() -> None:
    reg = load_registry(REGISTRY)
    doc = {
        "agent": "results", "allow": [{"record": ["dismiss"]}],
        "gates": {"dismiss": {"precondition": {
            "checks": ["analyteIsNotCritical"],
            "reads": [{"source": "critical-analyte-list", "freshness": "30d",
                       "onStale": "allow"}],
            "onUnavailable": "deny",
        }}},
    }
    findings = [f for f in lint(Policy.model_validate(doc), reg).findings if f.code == "13.22"]
    assert any(f.severity is Severity.WARN and "decoration" in f.message for f in findings)


def test_the_worked_policy_lints_clean_on_this_rule() -> None:
    reg = load_registry(REGISTRY)
    findings = [f for f in lint(Policy.model_validate(POLICY), reg).findings
                if f.code == "13.22"]
    assert findings == []
