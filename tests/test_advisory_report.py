"""The Advisory Report generator — the numbers, and the refusals.

The refusals are the load-bearing tests. A generator that averages across
enforcement modes, counts an unjudged reflex as a would-refuse, prints a zero
for a figure nobody supplied inputs for, or carries a row value onto a page,
produces a document that reads exactly like an honest one. So each of those is
pinned here rather than left to review.
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
from stonefold_core.enums import EnforcementMode
from stonefold_connectors import InMemoryConnector
from stonefold_gates.engine import DefaultGateEngine
from stonefold_store import InMemoryOutboxStore
from stonefold_report import (
    MixedDatasetError,
    NotAdvisoryError,
    build_report,
    render,
)
from tests.conftest import full_registry, load_schema

_DENY_DOC: dict[str, Any] = {
    "agent": "support",
    "allow": [{"observe": ["read"]}],
    "defaults": {"enforcement": "advisory"},
}
_HOLD_DOC: dict[str, Any] = {
    "agent": "support",
    "allow": [{"effect": ["sendEmail"]}, {"observe": ["read"]}],
    "gates": {"Email.sendEmail": {"requireApproval": {"approvers": "role:manager"}}},
    "defaults": {"enforcement": "advisory"},
}


def _run(doc: dict[str, Any], calls: list[RawCall], *, mode: EnforcementMode) -> Any:
    reg = full_registry()
    policy = load_policy(
        doc, reg, schema=load_schema(),
        advisory_permitted=mode is EnforcementMode.ADVISORY,
    )
    audit = InMemoryAuditSink()
    mem = InMemoryConnector({"Customer": [
        {"id": i, "owner_id": "alice" if i < 4 else "bob"} for i in range(1, 11)
    ]})
    for call in calls:
        enforce(
            call, Actor(id="alice"), Session(id="s1", correlation_id="corr-1"),
            registry=reg, audit=audit, policy=policy, gates=DefaultGateEngine(reg),
            outbox=InMemoryOutboxStore(audit=audit),
            connectors=Connectors({"sql": mem, "in_memory": mem, "email": mem}),
            enforcement=mode,
        )
    return audit


_EMAIL = RawCall(resource="Email", action="sendEmail", data={"to": "x@acme.example"})
_READ = RawCall(resource="Customer", action="read", data={})


# --- the numbers ---------------------------------------------------------
def test_the_report_counts_what_the_policy_would_have_done() -> None:
    audit = _run(_DENY_DOC, [_EMAIL, _EMAIL, _READ], mode=EnforcementMode.ADVISORY)

    report = build_report(audit.all_records(), agent="support")

    assert report.would_have.refused == 2  # the two emails, default-denied
    assert report.would_have.allowed == 1  # the read the policy permits
    assert report.coverage.observed == 3
    assert report.coverage.judged == 3
    assert report.coverage.ratio == 100.0


def test_coverage_leads_and_names_what_could_not_be_judged() -> None:
    audit = _run(
        _DENY_DOC,
        [_READ, RawCall(resource="NoSuchThing", action="frobnicate", data={})],
        mode=EnforcementMode.ADVISORY,
    )

    report = build_report(audit.all_records(), agent="support")
    text = render(report)

    assert report.coverage.unjudged == 1
    assert report.coverage.ratio == 50.0
    assert "not declared in the registry" in text
    # coverage is section 1, before anything the policy caught
    assert text.index("## 1. What we could see") < text.index("## 2. What the policy")


def test_distinct_questions_not_raw_holds() -> None:
    """The same question asked five times is one item in a queue. A report that
    says five overstates the staffing cost by five."""
    audit = _run(_HOLD_DOC, [_EMAIL] * 5, mode=EnforcementMode.ADVISORY)

    report = build_report(audit.all_records(), agent="support")

    assert report.questions.total_holds == 5
    # an approval-shaped hold is deliberately its own question per intent, so
    # these do not collapse — and the report says which kind it is counting
    assert report.questions.distinct + report.questions.unkeyed == 5


# --- the refusals --------------------------------------------------------
def test_a_mixed_dataset_is_refused_rather_than_averaged() -> None:
    """One figure spanning both modes describes no deployment that ever ran."""
    advisory = _run(_DENY_DOC, [_EMAIL], mode=EnforcementMode.ADVISORY)
    enforced_doc = {**_DENY_DOC}
    enforced_doc.pop("defaults")
    enforced = _run(enforced_doc, [_EMAIL], mode=EnforcementMode.ENFORCED)

    with pytest.raises(MixedDatasetError) as caught:
        build_report(
            advisory.all_records() + enforced.all_records(), agent="support"
        )

    assert "enforced" in str(caught.value) and "advisory" in str(caught.value)


def test_an_enforced_dataset_produces_no_report_at_all() -> None:
    enforced_doc = {**_DENY_DOC}
    enforced_doc.pop("defaults")
    audit = _run(enforced_doc, [_EMAIL], mode=EnforcementMode.ENFORCED)

    with pytest.raises(NotAdvisoryError):
        build_report(audit.all_records(), agent="support")


def test_an_unjudged_reflex_is_not_counted_as_a_would_refuse() -> None:
    """An unjudged record can carry advice — the fail-closed reflex enforcement
    would have had. Counting it inflates the number the report leads with."""
    audit = _run(
        _DENY_DOC,
        [RawCall(resource="NoSuchThing", action="frobnicate", data={})],
        mode=EnforcementMode.ADVISORY,
    )

    report = build_report(audit.all_records(), agent="support")

    assert report.coverage.unjudged == 1
    assert report.would_have.refused == 0  # it appears in coverage, and nowhere else


def test_a_missing_input_is_absent_never_zero() -> None:
    """A false-positive rate of zero is a finding. 'Nobody has reviewed this
    yet' is not the same claim, and the difference decides a deployment."""
    audit = _run(_DENY_DOC, [_EMAIL], mode=EnforcementMode.ADVISORY)

    report = build_report(audit.all_records(), agent="support")
    text = render(report)

    assert report.false_positives is None
    assert report.exclusive is None
    assert "Not reviewed" in text
    assert "Not joined" in text
    assert "0%" not in text.split("## 4.")[1].split("## 5.")[0]


def test_verdicts_produce_a_rate_and_downstream_produces_exclusivity() -> None:
    audit = _run(_DENY_DOC, [_EMAIL, _EMAIL], mode=EnforcementMode.ADVISORY)
    records = audit.all_records()
    rule = next(r.advised.rule for r in records if r.advised is not None)

    report = build_report(
        records, agent="support",
        verdicts={rule: "legitimate"},
        downstream_refused=["corr-1"],
    )

    assert report.reviewed == 2
    assert report.false_positives == 2
    assert report.exclusive == 0  # the estate refused the same correlation
    assert "false-positive rate" in render(report).lower()


def test_no_row_value_reaches_the_page() -> None:
    """The customer already has their data; our copy of it on a page is a
    liability rather than a service."""
    audit = _run(
        _DENY_DOC,
        [RawCall(resource="Email", action="sendEmail",
                 data={"to": "victim@secret-supplier.example", "amount": 4210.55})],
        mode=EnforcementMode.ADVISORY,
    )

    text = render(build_report(audit.all_records(), agent="support"))

    assert "victim@secret-supplier.example" not in text
    assert "4210.55" not in text
    assert "<string>" in text and "<decimal>" in text


def test_the_report_never_claims_prevention() -> None:
    audit = _run(_DENY_DOC, [_EMAIL], mode=EnforcementMode.ADVISORY)

    text = render(build_report(audit.all_records(), agent="support")).lower()

    for word in ("prevented", "blocked", "protected"):
        # the one licensed use is the disclaimer that says nothing was prevented
        assert text.count(word) <= 1
    assert "nothing in this window was prevented" in text


def test_every_number_carries_its_query() -> None:
    audit = _run(_DENY_DOC, [_EMAIL], mode=EnforcementMode.ADVISORY)

    text = render(build_report(audit.all_records(), agent="support"))

    assert "## Appendix — where each number comes from" in text
    assert "advised.decision='deny'" in text
    assert "count(distinct advised.dedupeKey)" in text


def test_the_same_question_asked_repeatedly_is_one_question() -> None:
    """The claim §3 exists to make. Twenty attempts at the same blocked thing is
    one item in a queue; a report that says twenty tells the customer to staff
    for twenty."""
    from stonefold_core.registry import load_registry
    from stonefold_gates.base import GateContext, check_hold

    registry = load_registry({
        "resources": {"Ticket": {"actions": {"close": {
            "kind": "record",
            "data": {"id": {"type": "string", "required": True}},
        }}}},
        "preconditionChecks": [
            {"name": "needsHuman", "holdCapable": True,
             "reasonCodes": {"NEEDS_HUMAN": "escalate"}},
        ],
    })
    doc = {
        "agent": "desk",
        "allow": [{"record": ["close"]}],
        "gates": {"close": {"precondition": {
            "checks": ["needsHuman"], "resolvers": "role:supervisor",
        }}},
        "defaults": {"enforcement": "advisory"},
    }
    policy = load_policy(doc, registry, schema=load_schema(), advisory_permitted=True)
    audit = InMemoryAuditSink()

    def _needs_human(gctx: GateContext) -> Any:
        return check_hold("NEEDS_HUMAN", {"ticket": gctx.resolved.data.get("id")})

    engine = DefaultGateEngine(registry, preconditions={"needsHuman": _needs_human})
    mem = InMemoryConnector({})
    for _ in range(6):  # the same ticket, six times
        enforce(
            RawCall(resource="Ticket", action="close", data={"id": "T-1"}),
            Actor(id="alice"), Session(id="s1", correlation_id="c1"),
            registry=registry, audit=audit, policy=policy, gates=engine,
            outbox=InMemoryOutboxStore(audit=audit),
            connectors=Connectors({"in_memory": mem}),
            enforcement=EnforcementMode.ADVISORY,
        )
    # ...and a different ticket once: a distinct question, not a repeat
    enforce(
        RawCall(resource="Ticket", action="close", data={"id": "T-2"}),
        Actor(id="alice"), Session(id="s1", correlation_id="c1"),
        registry=registry, audit=audit, policy=policy, gates=engine,
        outbox=InMemoryOutboxStore(audit=audit),
        connectors=Connectors({"in_memory": mem}),
        enforcement=EnforcementMode.ADVISORY,
    )

    report = build_report(audit.all_records(), agent="desk")

    assert report.questions.total_holds == 7
    assert report.questions.total_questions == 2  # T-1 asked six times, T-2 once
    assert report.questions.repeats_per_question == 3.5
    assert report.questions.busiest is not None
    assert report.questions.busiest[1] == 6
    assert "Questions a person would have been asked: **2**" in render(report)
