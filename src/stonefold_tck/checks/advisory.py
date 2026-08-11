# SPDX-License-Identifier: Apache-2.0
"""A1–A9 — the advisory profile (v0.4; profile ``advisory``).

An advisory deployment sits in the real traffic path, decides every action
exactly as it would when enforcing, records every verdict, and refuses nothing
but a kill order. Its audit is the counterfactual: what the policy *would* have
done to traffic nobody staged.

The whole profile rests on one property, and A8 is the check for it: an advisory
run and an enforcing run of the same fixture reach the same verdicts. Without
that, "would have held 27" is not a measurement of anything, and the rest of
these checks are decoration. A gateway that fails A8 and passes the others is
more dangerous than one that fails them all, because its report reads correct.

A2 is the second load-bearing one. An agent that learns which of its actions are
ungoverned is no longer producing the traffic being measured, so the advisory
verdict must reach the audit and nothing else.
"""

from __future__ import annotations

from stonefold_tck.checks import PROFILE_ADVISORY, check, expect
from stonefold_tck.checks._util import SESSION, effects_of, pay, setup, submit
from stonefold_tck.driver import (
    CAP_ADVISORY,
    CAP_AUDIT,
    CAP_KILL,
    CAP_PER_ITEM,
    CAP_SCOPE_REASSERT,
    CAP_STAGING,
    ConformanceDriver,
    Operation,
)
from stonefold_tck.fixtures import BLOCKED_ITEMS, POLICY_ADVISORY, POLICY_PER_ITEM, TCK_POLICY


def _advisory_variant(policy: str) -> str:
    """The same policy document, decided the same way, with the deployment
    doing nothing about the verdicts. One line different, so a check comparing
    the two modes is comparing the modes and nothing else."""
    return policy.replace(
        "defaults: { failureMode: closed, audit: full }",
        "defaults: { failureMode: closed, audit: full, enforcement: advisory }",
    )

# Blocked by the denylist gate in both fixtures: a refusal every conforming
# gateway already produces, so the profile tests the MODE and not the policy.
_BLOCKED_PAY = Operation(
    resource="Payment",
    action="pay",
    data={"amount": 100, "destinationCountry": "XX", "payeeId": "PE-1"},
)
# Held by requireApproval in both fixtures (1000 < amount <= 5000).
_HELD_PAY = Operation(
    resource="Payment",
    action="pay",
    data={"amount": 2000, "destinationCountry": "DE", "payeeId": "PE-2"},
)


def _records(driver: ConformanceDriver, action: str) -> list:
    return [a for a in driver.audit() if a.action == action]


@check(
    "A1",
    "an action the policy denies executes anyway, and the record keeps the deny",
    PROFILE_ADVISORY,
    requires=[CAP_ADVISORY, CAP_AUDIT],
)
def a1_a_denied_action_goes_through(driver: ConformanceDriver) -> None:
    setup(driver, policy=POLICY_ADVISORY)
    result = submit(driver, _BLOCKED_PAY)

    expect(
        result.decision == "allow",
        f"the deployment answered {result.decision!r}. An advisory deployment refuses "
        "nothing but a kill order — a refusal here is the promise the customer "
        "routed their traffic on, broken",
    )
    records = _records(driver, "pay")
    expect(bool(records), "the action was not audited at all")
    record = records[-1]
    expect(
        record.advised_decision == "deny",
        f"the record's advised decision is {record.advised_decision!r}; the policy "
        "denied this action and the counterfactual is the entire product",
    )
    expect(
        bool(record.advised_rule),
        "the record does not say WHICH rule would have refused it, so the report "
        "cannot tell the customer what to fix",
    )


@check(
    "A2",
    "the agent-facing result carries no advisory trace",
    PROFILE_ADVISORY,
    requires=[CAP_ADVISORY],
)
def a2_the_agent_learns_nothing(driver: ConformanceDriver) -> None:
    setup(driver, policy=POLICY_ADVISORY)
    result = submit(driver, _BLOCKED_PAY)

    expect(result.decision == "allow", "precondition: the action went through")
    view = (result.agent_view or "").lower()
    for leak in ("advised", "wouldrefuse", "enforcement", "coverage"):
        expect(
            leak not in view,
            f"the agent view names {leak!r}. An agent that learns which of its "
            "actions are ungoverned stops producing the traffic being measured, "
            "and the measurement is the deliverable",
        )
    expect(
        not result.reason_code,
        f"the result carries reason code {result.reason_code!r} — an allow the "
        "agent cannot distinguish from any other allow is the point",
    )


@check(
    "A3",
    "a kill order still halts under advisory",
    PROFILE_ADVISORY,
    requires=[CAP_ADVISORY, CAP_KILL],
)
def a3_the_cord_still_works(driver: ConformanceDriver) -> None:
    """The single lever an advisory deployment keeps. If the operator cannot stop
    an agent during a watch-only pilot, the pilot is not safe to run."""
    setup(driver, policy=POLICY_ADVISORY)
    driver.kill(scope="session", session_id=SESSION)
    result = submit(driver, _HELD_PAY)

    expect(
        result.decision == "halt",
        f"a killed agent's action answered {result.decision!r} under advisory. A "
        "kill order is an operator pulling the cord, not a policy verdict, and it "
        "is the reason a customer can agree to route traffic through the gateway "
        "at all",
    )


@check(
    "A4",
    "an advised hold stages nothing",
    PROFILE_ADVISORY,
    requires=[CAP_ADVISORY, CAP_STAGING],
)
def a4_an_advised_hold_queues_no_question(driver: ConformanceDriver) -> None:
    """Nobody is going to answer a hold in a deployment that stops nothing, and
    the effect has already happened. The question is counted in the report, not
    parked in a human's queue."""
    setup(driver, policy=POLICY_ADVISORY)
    result = submit(driver, _HELD_PAY)

    expect(
        result.decision == "allow",
        f"the held action answered {result.decision!r}; advisory holds nothing",
    )
    # A ticket may well exist — an allowed effect still stages (invariant 4), and
    # advisory does not change how an allowed action runs. What must not exist is
    # a QUESTION: the effect leaves without anyone approving anything.
    driver.dispatch_once()
    expect(
        effects_of(driver, "pay") == 1,
        "the effect did not leave the gateway without an approval. Under advisory "
        "the action already happened, so a hold parks a question nobody will "
        "answer while the human queue fills with them",
    )


@check(
    "A5",
    "every record says which deployment produced it",
    PROFILE_ADVISORY,
    requires=[CAP_ADVISORY, CAP_AUDIT],
)
def a5_records_name_their_mode(driver: ConformanceDriver) -> None:
    """A record that could have come from either deployment is evidence of
    nothing, and a report averaging the two is not a report."""
    setup(driver, policy=POLICY_ADVISORY)
    submit(driver, _BLOCKED_PAY)

    for record in driver.audit():
        expect(
            record.enforcement == "advisory",
            f"a record from an advisory deployment says enforcement="
            f"{record.enforcement!r}. Every record, including the ones written "
            "after the decision, or the dataset is mixed and no figure in it can "
            "be trusted",
        )


@check(
    "A6",
    "an action the gateway could not judge is recorded unjudged, never as an allow",
    PROFILE_ADVISORY,
    requires=[CAP_ADVISORY, CAP_AUDIT],
)
def a6_coverage_is_reported(driver: ConformanceDriver) -> None:
    """The honest half. A pilot reporting 94% coverage is telling the truth and
    starting a real conversation about the other 6%; one reporting only its
    catches is doing what we accuse everyone else of."""
    setup(driver, policy=POLICY_ADVISORY)
    submit(driver, Operation(resource="NoSuchResource", action="frobnicate", data={}))

    records = [a for a in driver.audit() if a.resource == "NoSuchResource"]
    expect(
        bool(records),
        "an action the gateway could not resolve left no record at all — the "
        "coverage number the report opens with is built from these",
    )
    record = records[-1]
    expect(
        record.coverage == "unjudged",
        f"the record says coverage={record.coverage!r} for an action no policy "
        "could judge. Counting it as judged overstates the one number the report "
        "leads with",
    )
    expect(
        record.decision != "allow" or record.coverage == "unjudged",
        "an unjudged action was counted as an advisory allow",
    )


@check(
    "A7",
    "aggregates accumulate under advisory",
    PROFILE_ADVISORY,
    requires=[CAP_ADVISORY, CAP_AUDIT],
)
def a7_aggregates_still_count(driver: ConformanceDriver) -> None:
    """Everything goes through, so everything counts. Without this a spend limit
    never trips and the report undercounts its own value."""
    setup(driver, policy=POLICY_ADVISORY)
    rated = Operation(
        resource="Payment", action="pay",
        data={"amount": 100, "destinationCountry": "DE", "payeeId": "PE-RATE"},
    )
    for _ in range(3):  # the fixture's rate gate allows 2/hour for this payee
        submit(driver, rated)

    advised = [
        a for a in _records(driver, "pay") if a.advised_decision == "deny"
    ]
    expect(
        bool(advised),
        "three actions past a 2/hour limit produced no advised refusal. The gate "
        "counted nothing, so the report would tell the customer the limit was "
        "never approached",
    )


@check(
    "A8",
    "advisory and enforcing runs of the same fixture reach the same verdicts",
    PROFILE_ADVISORY,
    requires=[CAP_ADVISORY, CAP_AUDIT],
)
def a8_the_verdicts_are_identical(driver: ConformanceDriver) -> None:
    """The property the whole profile rests on, and the cheapest to check: same
    fixtures, two modes, compare. A gateway that fails this one and passes the
    rest is the dangerous case — its report reads correct."""
    setup(driver, policy=TCK_POLICY)
    enforced_result = submit(driver, _BLOCKED_PAY)
    enforced_records = _records(driver, "pay")
    expect(bool(enforced_records), "the enforcing run audited nothing")
    enforced_rule = enforced_records[-1].reason

    setup(driver, policy=POLICY_ADVISORY)
    submit(driver, _BLOCKED_PAY)
    advisory_records = _records(driver, "pay")
    expect(bool(advisory_records), "the advisory run audited nothing")
    advised = advisory_records[-1]

    expect(
        advised.advised_decision == enforced_result.decision,
        f"enforcing decided {enforced_result.decision!r} and advisory recorded "
        f"{advised.advised_decision!r} for the same fixture. The two deployments "
        "must compute the same verdict or 'would have held' measures nothing",
    )
    expect(
        advised.advised_rule == enforced_rule,
        f"enforcing refused on {enforced_rule!r}; advisory recorded "
        f"{advised.advised_rule!r}. Same verdict, same reason, or the report "
        "names the wrong rule to the customer who has to fix it",
    )


@check(
    "A9",
    "a batch enforcement would have refused commits whole, and says so",
    PROFILE_ADVISORY,
    requires=[CAP_ADVISORY, CAP_AUDIT],
)
def a9_a_refused_batch_commits(driver: ConformanceDriver) -> None:
    """A batch's atomicity is a property of the batch, so no single operation's
    record can carry it — and enforcing would have refused all of it."""
    from stonefold_tck.driver import CAP_BATCH

    if CAP_BATCH not in driver.capabilities():
        return  # batches are a separate capability; nothing to assert here
    setup(driver, policy=POLICY_ADVISORY)
    clean = Operation(
        resource="Payment", action="pay",
        data={"amount": 10, "destinationCountry": "DE", "payeeId": "PE-9"},
    )
    batch = driver.submit_batch(
        __import__("stonefold_tck.checks._util", fromlist=["ALICE"]).ALICE,
        "tck-s1",
        [clean, _BLOCKED_PAY],
    )

    expect(
        batch.decision == "allow",
        f"the batch answered {batch.decision!r}. Advisory refuses nothing, so a "
        "batch enforcement would have refused still commits whole",
    )
    advised = [a for a in _records(driver, "pay") if a.advised_decision == "deny"]
    expect(
        bool(advised),
        "no operation's record carries the refusal enforcement would have made, "
        "so the report cannot say the batch would have been refused atomically",
    )


@check(
    "A10",
    "an item-bearing call applies every item, and the record names the ones "
    "enforcement would have refused",
    PROFILE_ADVISORY,
    requires=[CAP_ADVISORY, CAP_AUDIT, CAP_PER_ITEM],
)
def a10_items_apply_and_the_record_breaks_them_down(driver: ConformanceDriver) -> None:
    """Every item is applied, so the call writes one record where enforcement
    would have written one per refusal plus one for the applied subset. A single
    ``advised`` cannot hold N verdicts, so the breakdown has to be on it —
    otherwise the report can say how many calls diverged but not which items,
    and the customer cannot act on it."""
    setup(driver, policy=_advisory_variant(POLICY_PER_ITEM))
    close = Operation(
        resource="Queue", action="closeMany",
        data={"itemIds": ["Q-1", BLOCKED_ITEMS[0], "Q-2"]},
    )
    result = submit(driver, close)

    expect(
        result.decision == "allow",
        f"the call answered {result.decision!r}; under advisory every item applies",
    )
    expect(
        sorted(result.applied) == sorted(["Q-1", BLOCKED_ITEMS[0], "Q-2"]),
        f"applied is {list(result.applied)} — advisory applies the blocked item too, "
        "or the estate is not behaving as it would without the gateway",
    )
    for verdict in result.items:
        expect(
            verdict.get("decision") == "allow",
            f"item {verdict.get('item')!r} came back {verdict.get('decision')!r}. "
            "Reading the items side by side must not tell the agent which ones the "
            "policy dislikes — that is an enumeration oracle, one call per read",
        )
    records = [a for a in driver.audit() if a.action == "closeMany"]
    expect(len(records) == 1, f"{len(records)} records for one applied call")
    detail = records[0].item_advice
    expect(
        detail is not None,
        "the record carries no per-item breakdown, so the report can say the call "
        "diverged but not which item did",
    )
    assert detail is not None
    items = [str(entry.get("item")) for entry in detail.get("items", [])]
    expect(
        BLOCKED_ITEMS[0] in items,
        f"the breakdown names {items}; the blocked item is the one enforcement "
        "would have refused",
    )


@check(
    "A11",
    "a scoped read returns everything, and the record counts what scope would "
    "have removed",
    PROFILE_ADVISORY,
    requires=[CAP_ADVISORY, CAP_AUDIT],
)
def a11_scope_is_measured_not_applied(driver: ConformanceDriver) -> None:
    """Narrowing the read would hand the agent fewer rows than it gets today,
    and the traffic being measured would stop being the estate's own. So the read
    runs wide and the record carries the number — the most uncomfortable line in
    the report, and the most persuasive."""
    setup(driver, policy=POLICY_ADVISORY)
    result = submit(driver, Operation(resource="Widget", action="read", data={}))
    rows = list(result.rows or [])

    expect(
        len(rows) == 10,
        f"the read returned {len(rows)} of 10 seeded rows. An advisory deployment "
        "that narrows is enforcing, quietly, on the one surface where the agent "
        "cannot tell",
    )
    records = [a for a in driver.audit() if a.action == "read" and a.resource == "Widget"]
    expect(bool(records), "the read was not audited")
    measured = records[-1].scope_would_remove
    expect(
        measured is not None,
        "the record does not say what scope would have removed, so the pilot "
        "widened the agent's reach and reported nothing about it",
    )
    assert measured is not None
    expect(
        measured.get("removed") == 7,
        f"the record says scope would have removed {measured.get('removed')!r} of "
        "the 10 rows; 7 belong to another owner",
    )


@check(
    "A12",
    "an effect whose target leaves scope between decision and dispatch still "
    "dispatches, and the settle record says what enforcement would have done",
    PROFILE_ADVISORY,
    requires=[CAP_ADVISORY, CAP_AUDIT, CAP_STAGING, CAP_SCOPE_REASSERT],
)
def a12_dispatch_scope_is_measured_not_applied(driver: ConformanceDriver) -> None:
    """The refusal written by the worker rather than by the decision path. An
    advisory deployment that re-asserts scope at dispatch stops a customer's
    effect at the last possible moment — the one thing it promised not to do, in
    the place the translation never reaches."""
    setup(driver, policy=POLICY_ADVISORY)
    result = submit(driver, pay(500, target="P1"))
    expect(result.decision == "allow", "precondition: the effect was staged")

    # the race the enforcing profile pins: the target moves to another tenant
    driver.seed("Payment", [{"id": "P1", "tenant": "t2"}, {"id": "P2", "tenant": "t2"}])
    driver.dispatch_once()

    expect(
        effects_of(driver, "pay") == 1,
        "the effect never left. Re-asserting scope at dispatch is a control, not "
        "execution machinery: under advisory it refuses a customer's payment "
        "after the deployment promised it would refuse nothing",
    )
    settles = [
        a for a in driver.audit() if a.action == "pay" and a.outcome == "success"
    ]
    expect(bool(settles), "the dispatch was not audited")
    expect(
        settles[-1].advised_decision == "deny",
        f"the settle record's advised decision is "
        f"{settles[-1].advised_decision!r}; enforcement would have refused this "
        "effect as scope-lost, and a report that cannot say so undercounts what "
        "the policy was worth",
    )
