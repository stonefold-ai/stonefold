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


def _run(
    doc: dict[str, Any], calls: list[RawCall], *, mode: EnforcementMode,
    scoped: bool = False,
) -> Any:
    from stonefold_core.scope import make_scope_resolver

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
            scopes=make_scope_resolver(policy) if scoped else None,
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
        verdicts={f"{rule} @ Email.sendEmail": "legitimate"},
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


def test_a_dispatched_effect_is_one_action_not_two() -> None:
    """A dispatched effect writes a decision record and a settle record. The
    review found the report counting both as actions — 2 payments read as 4
    observed and a scope-refused one as 2 would-refusals. Decisions are the
    actions; settles are what happened to them."""
    from stonefold_core.scope import make_scope_resolver
    from stonefold_store.dispatch import DispatchWorker

    reg = full_registry()
    doc = {
        "agent": "pay", "allow": [{"effect": ["pay"]}],
        "scope": {"Payment": "tenantOf"},
        "defaults": {"enforcement": "advisory"},
    }
    policy = load_policy(doc, reg, schema=load_schema(), advisory_permitted=True)
    audit = InMemoryAuditSink()
    outbox = InMemoryOutboxStore(audit=audit)
    conn = InMemoryConnector({"Payment": [
        {"id": "P-1", "tenant_id": "T1"}, {"id": "P-2", "tenant_id": "T2"},
    ]})
    conns = Connectors({"sql": conn, "in_memory": conn, "email": conn})
    for pid in ("P-1", "P-2"):  # P-2 is another tenant's: enforcement would refuse
        enforce(
            RawCall(resource="Payment", action="pay", data={"id": pid, "amount": 100}),
            Actor(id="alice", claims={"tenant": "T1"}),
            Session(id="s1", correlation_id="c1"),
            registry=reg, audit=audit, policy=policy, gates=DefaultGateEngine(reg),
            scopes=make_scope_resolver(policy), outbox=outbox, connectors=conns,
            enforcement=EnforcementMode.ADVISORY,
        )
    worker = DispatchWorker(outbox, conns, registry=reg,
                            scopes=make_scope_resolver(policy))
    assert worker.drain() == 2

    report = build_report(audit.all_records(), agent="pay")

    assert report.coverage.observed == 2  # actions, not records
    assert report.would_have.refused == 1  # the tenant-crossing payment, once
    # the settles are not lost — they are the outcomes view
    assert report.outcomes.settled == 2
    assert report.outcomes.moved_out_of_scope == 1


def test_a_scoped_write_is_not_reported_as_a_read() -> None:
    """Section 5 is about reads. A scoped write returns a receipt, and stamping
    'could not count rows' on every one buries the reads under noise."""
    doc = {
        "agent": "support",
        "allow": [{"record": ["update"]}],
        "scope": {"Customer": "assignedToCurrentUser"},
        "defaults": {"enforcement": "advisory"},
    }
    audit = _run(doc, [RawCall(resource="Customer", action="update",
                               data={"id": 1, "status": "vip"})],
                 mode=EnforcementMode.ADVISORY, scoped=True)

    report = build_report(audit.all_records(), agent="support")

    assert report.scope.reads == 0
    for record in audit.all_records():
        assert record.scopeWouldRemove is None


def test_the_report_names_when_the_work_happened() -> None:
    audit = _run(_DENY_DOC, [_EMAIL, _READ], mode=EnforcementMode.ADVISORY)

    report = build_report(audit.all_records(), agent="support")

    assert report.activity.days_with_traffic == 1
    assert report.activity.busiest is not None
    assert report.activity.busiest[1] == 2


def test_section_nine_recommends_nothing_without_the_review() -> None:
    """The conversion path is ranked by evidence, and an unreviewed rule has
    none: its false positives simply have not been looked for."""
    audit = _run(_DENY_DOC, [_EMAIL], mode=EnforcementMode.ADVISORY)

    text = render(build_report(audit.all_records(), agent="support"))

    assert "## 9. What we would turn on first" in text
    assert "Nothing is ready to enforce yet" in text
    assert "Advisory continues to run" in text


def test_the_html_edition_is_self_contained_and_leaks_nothing() -> None:
    from stonefold_report import render_html

    audit = _run(
        _DENY_DOC,
        [RawCall(resource="Email", action="sendEmail",
                 data={"to": "victim@secret-supplier.example", "amount": 4210.55})],
        mode=EnforcementMode.ADVISORY,
    )

    page = render_html(build_report(audit.all_records(), agent="support"))

    # one file, mail-room physics: no scripts, no external fetches of any kind
    assert "<script" not in page.lower()
    assert "http://" not in page and "https://" not in page
    # same redaction rules as the figures: no row value reaches the page
    assert "victim@secret-supplier.example" not in page
    assert "4210.55" not in page
    # the charts are real and the absences are stated, not zeroed
    assert "<svg" in page
    assert "Not reviewed" in page and "Not joined" in page
    assert "would have done" in page


def test_the_html_escapes_what_the_policy_wrote() -> None:
    """Rule names come from policy documents; a policy author must not be able
    to script the customer's report."""
    from stonefold_report.figures import (
        ActivityFigures, CoverageFigures, Disclosures, OutcomeFigures,
        QuestionFigures, Report, RuleLine, ScopeFigures, WouldHaveFigures,
        WorksheetLine,
    )
    from stonefold_report.html import render_html

    hostile = "<script>alert(1)</script>"
    report = Report(
        agent=hostile,
        coverage=CoverageFigures(observed=1, judged=1, unjudged=0, by_cause=()),
        would_have=WouldHaveFigures(
            allowed=0, refused=1, held=0, halted=0,
            by_rule=(RuleLine(rule=hostile, decision="deny", count=1,
                              actions=(hostile,), examples=()),),
            batches_refused=0, item_calls=0, item_refusals=0,
        ),
        questions=QuestionFigures(distinct=0, total_holds=0, busiest=None, unkeyed=0),
        scope=ScopeFigures(reads=0, rows_returned=0, rows_removed=0,
                           widest=None, partial_reads=0, unmeasured=()),
        activity=ActivityFigures(days_with_traffic=0, by_day=(), busiest=None),
        outcomes=OutcomeFigures(settled=0, failed=0, cancelled=0,
                                moved_out_of_scope=0),
        disclosures=Disclosures(kill_orders=0, kill_unavailable=0,
                                first=None, last=None),
        worksheet=(WorksheetLine(rule=hostile, resource="R", action="a", count=1),),
        false_positives=None, reviewed=None, exclusive=None,
    )

    page = render_html(report)

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_both_editions_lead_with_the_computed_summary() -> None:
    from stonefold_report import render_html

    audit = _run(_DENY_DOC, [_EMAIL, _READ], mode=EnforcementMode.ADVISORY)
    report = build_report(audit.all_records(), agent="support")

    md, page = render(report), render_html(report)

    for text in (md, page):
        assert "day(s) with traffic" in text
        assert "could be judged by the policy" in text
    # the summary leads, the disclaimer stays — findings before caveats
    assert page.index("day(s) with traffic") < page.index("Nothing in this window")


def test_the_activity_strip_draws_the_silence() -> None:
    """A burst then a quiet week and steady work all fortnight are different
    findings; a strip that draws only busy days compresses them into one."""
    from datetime import datetime, timedelta, timezone

    audit = _run(_DENY_DOC, [_EMAIL, _EMAIL, _EMAIL], mode=EnforcementMode.ADVISORY)
    start = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)
    spread = [
        r.model_copy(update={"timestamp": start + timedelta(days=offset)})
        for r, offset in zip(audit.all_records(), (0, 0, 4))
    ]

    report = build_report(spread, agent="support")

    assert report.activity.days_with_traffic == 2
    assert len(report.activity.by_day) == 5  # the three quiet days are drawn
    assert [count for _d, count in report.activity.by_day] == [2, 0, 0, 0, 1]


def test_the_page_answers_who_when_and_from_what() -> None:
    """Provenance is table stakes for an evidence document: an auditor's first
    questions are when this was produced, for whom, and from how much data."""
    from datetime import datetime, timezone

    from stonefold_report import render_html

    audit = _run(_DENY_DOC, [_EMAIL], mode=EnforcementMode.ADVISORY)
    report = build_report(audit.all_records(), agent="support")

    page = render_html(
        report,
        prepared_for="Meridian Supply GmbH",
        generated_at=datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc),
    )

    assert "prepared for <b>Meridian Supply GmbH</b>" in page
    assert "Confidential" in page
    assert "Generated 2026-08-11 15:00 UTC" in page
    assert "decision record(s)" in page


def test_the_worksheet_can_be_completed_with_a_pen() -> None:
    from stonefold_report import render_html

    audit = _run(_DENY_DOC, [_EMAIL], mode=EnforcementMode.ADVISORY)
    page = render_html(build_report(audit.all_records(), agent="support"))

    assert "How to complete this" in page
    assert page.count('class="box"') == 3  # one line, three tick boxes
    assert "Ordinary work" in page and "Correctly refused" in page


def test_rule_codes_carry_plain_words() -> None:
    """A CFO should not need the policy grammar to read their own report."""
    from stonefold_report import render_html

    audit = _run(_DENY_DOC, [_EMAIL], mode=EnforcementMode.ADVISORY)
    page = render_html(build_report(audit.all_records(), agent="support"))

    assert "default-deny" in page  # the code stays, for the engineer
    assert "not permitted by the policy" in page  # the words, for the reader


def test_section_nine_keeps_its_emphasis_in_html() -> None:
    from stonefold_report import render_html

    audit = _run(_DENY_DOC, [_EMAIL], mode=EnforcementMode.ADVISORY)
    page = render_html(build_report(audit.all_records(), agent="support"))

    nine = page.split("9 · What we would turn on first")[1]
    assert "<b>Nothing is ready to enforce yet" in nine
    assert "**" not in nine  # converted, not leaked


def test_the_appendix_travels_with_the_page() -> None:
    from stonefold_report import render_html

    audit = _run(_DENY_DOC, [_EMAIL], mode=EnforcementMode.ADVISORY)
    page = render_html(build_report(audit.all_records(), agent="support"))

    assert "<details>" in page  # no-script collapsible
    assert "advised.decision=&#x27;deny&#x27;" in page or "advised.decision='deny'" in page
