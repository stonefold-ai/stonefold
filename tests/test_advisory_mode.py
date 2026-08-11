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
from collections.abc import Mapping

from stonefold_core.enums import Coverage, EnforcementMode
from stonefold_core.models import ResolvedAction
from stonefold_core.pipeline import ADVISORY_RULE, ITEMS_OVER_CEILING
from stonefold_core.registry import load_registry
from stonefold_core.scope import ScopePredicate, make_scope_resolver
from stonefold_connectors import InMemoryConnector
from stonefold_gates.engine import DefaultGateEngine
from stonefold_store import InMemoryOutboxStore
from stonefold_store.kill_memory import InMemoryKillStore
from tests.conftest import full_registry, load_schema

# The item-bearing fixture is the per-item suite's own worklist: same items, same
# gates, same clinician checks. Sharing it is the point — what differs between
# that suite and these tests is the mode and nothing else, which is the only way
# "the verdicts are identical" is worth asserting.
from tests.test_v03_per_item import ACTOR as WORKLIST_ACTOR
from tests.test_v03_per_item import RUN as WORKLIST_RUN
from tests.test_v03_per_item import _mark, _world


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


# --- coverage: the honest number ------------------------------------------
def test_an_unresolvable_action_is_unjudged_not_allowed() -> None:
    """The gateway cannot forward what it cannot address: the deny stands,
    marked as the coverage case — never counted as an advisory allow."""
    result, audit, _ = _run(
        _ALLOW_DOC,
        resource="NoSuchResource",
        action="frobnicate",
        enforcement=EnforcementMode.ADVISORY,
    )

    assert result.decision is Decision.DENY
    record = audit.records[-1]
    assert record.enforcement is EnforcementMode.ADVISORY
    assert record.coverage is Coverage.UNJUDGED
    assert record.advised is None  # no judgement was reached, so none is advised


def test_a_judged_action_says_judged() -> None:
    for doc, expect_advice in ((_ALLOW_DOC, False), (_DENY_DOC, True)):
        _, audit, _ = _run(
            doc,
            resource="Email",
            action="sendEmail",
            enforcement=EnforcementMode.ADVISORY,
        )
        record = audit.records[-1]
        assert record.coverage is Coverage.JUDGED
        assert (record.advised is not None) is expect_advice


def test_a_killed_batch_still_stamps_the_mode() -> None:
    """A kill refuses an advisory batch for real — and the records still say
    which deployment they came from, or the dataset shows phantom enforced
    traffic from a deployment that has none."""
    from stonefold_core import KillScope
    from stonefold_store.kill_memory import InMemoryKillStore

    kill = InMemoryKillStore()
    kill.issue(KillScope.for_session("s1"), issued_by="operator")
    reg = full_registry()
    policy = load_policy(_ALLOW_DOC, reg, schema=load_schema())
    audit = InMemoryAuditSink()
    result = enforce_batch(
        [
            RawCall(resource="Customer", action="read"),
            RawCall(resource="Email", action="sendEmail"),
        ],
        Actor(id="alice"),
        Session(id="s1", correlation_id="corr-1"),
        registry=reg,
        audit=audit,
        policy=policy,
        gates=DefaultGateEngine(reg),
        kill=kill,
        enforcement=EnforcementMode.ADVISORY,
    )

    assert result.decision is Decision.HALT
    assert audit.records, "a refused batch still audits every operation"
    for record in audit.records:
        assert record.enforcement is EnforcementMode.ADVISORY
        assert record.advised is None  # the halt happened; nothing is advice


# --- item-bearing actions ------------------------------------------------
# An item-bearing call is where advisory and enforcement look least alike: the
# enforcing run writes a record per refused item and one for the applied subset,
# the advisory run applies everything and writes one. The verdicts underneath are
# still identical, and ``itemAdvice`` is where they survive.
def _worklist(
    *items: str,
    enforcement: EnforcementMode,
    kill: Any = None,
    world: dict[str, Any] | None = None,
) -> Any:
    world = world if world is not None else _world()
    result = enforce(
        _mark(*items), WORKLIST_ACTOR, WORKLIST_RUN, **world,
        kill=kill, enforcement=enforcement,
    )
    return result, world


def test_advisory_applies_every_item() -> None:
    """W-13 needs a clinician and W-15 is ambiguous; under advisory both go
    through with the rest, because the estate behaves as if we were not there."""
    result, _ = _worklist(
        "W-1", "W-13", "W-15", "W-2", enforcement=EnforcementMode.ADVISORY
    )

    assert result.decision is Decision.ALLOW
    assert result.applied == ("W-1", "W-13", "W-15", "W-2")
    assert {v.decision for v in result.items} == {Decision.ALLOW}
    # No item is distinguishable from the others (TCK A-2, per item): the applied
    # items went through as one call and answer with the call's rule, so reading
    # the items side by side does not enumerate the ones the policy dislikes.
    assert {v.rule for v in result.items} == {result.rule}
    assert all(v.reason_code == "" and v.ticket is None for v in result.items)


def test_an_all_advised_call_does_not_name_the_items_either() -> None:
    """Every item was refusable, so the call answers ``advisory`` — once, for the
    call. Which item earned it is in the audit, not in the response."""
    result, _ = _worklist("W-13", "W-14", enforcement=EnforcementMode.ADVISORY)

    assert result.decision is Decision.ALLOW
    assert result.rule == ADVISORY_RULE
    assert {v.rule for v in result.items} == {ADVISORY_RULE}


def test_the_applied_call_carries_the_item_breakdown() -> None:
    """One record for one call — and ``advised`` alone would flatten four
    verdicts into one, so the items keep their own."""
    _, world = _worklist(
        "W-1", "W-13", "W-15", "W-2", enforcement=EnforcementMode.ADVISORY
    )

    records = world["audit"].records
    assert len(records) == 1, "every item was applied, so the call is one record"
    record = records[0]
    assert record.parameters["itemIds"] == ["W-1", "W-13", "W-15", "W-2"]
    assert record.enforcement is EnforcementMode.ADVISORY
    # what the ACTOR would have been told about the call
    assert record.advised is not None
    assert record.advised.decision is Decision.DENY
    # and which items it flattens
    assert record.itemAdvice is not None
    assert record.itemAdvice["wouldApply"] == 2
    assert record.itemAdvice["wouldRefuse"] == 2
    entries = record.itemAdvice["items"]
    assert [e["item"] for e in entries] == ["W-13", "W-15"]
    assert [e["decision"] for e in entries] == ["deny", "hold"]
    assert [e["reasonCode"] for e in entries] == [
        "ITEM_NEEDS_CLINICIAN", "ITEM_AMBIGUOUS",
    ]


def test_a_call_no_item_diverges_on_carries_no_item_advice() -> None:
    """``itemAdvice`` marks divergence, item by item, as ``advised`` does for the
    call: a clean call is a plain advisory allow."""
    _, world = _worklist("W-1", "W-2", enforcement=EnforcementMode.ADVISORY)

    record = world["audit"].records[-1]
    assert record.advised is None
    assert record.itemAdvice is None
    assert record.coverage is Coverage.JUDGED


def test_an_advised_item_hold_stages_no_ticket() -> None:
    """D-A5 per item: nobody is going to answer a question about an item that
    has already gone through."""
    enforced, enforced_world = _worklist(
        "W-1", "W-15", enforcement=EnforcementMode.ENFORCED
    )
    assert enforced.decision is Decision.HOLD
    assert enforced_world["outbox"].list_by_state(PendingState.PENDING_APPROVAL)

    result, world = _worklist("W-1", "W-15", enforcement=EnforcementMode.ADVISORY)

    assert result.decision is Decision.ALLOW
    assert world["outbox"].list_by_state(PendingState.PENDING_APPROVAL) == []
    assert [v.ticket for v in result.items] == [None, None]
    # The question is not lost — it is counted, which is what the report needs.
    assert world["audit"].records[-1].itemAdvice["items"][0]["decision"] == "hold"


def test_advisory_and_enforced_item_verdicts_are_identical() -> None:
    """TCK A-8, per item: same fixture, two modes, the same verdict for every
    item — including which items would have been applied."""
    items = ("W-1", "W-13", "W-15", "W-2", "W-14")
    enforced, _ = _worklist(*items, enforcement=EnforcementMode.ENFORCED)
    _, world = _worklist(*items, enforcement=EnforcementMode.ADVISORY)

    record = world["audit"].records[-1]
    assert record.advised is not None
    assert record.advised.decision is enforced.decision  # the same envelope
    assert record.itemAdvice is not None
    assert record.itemAdvice["wouldApply"] == len(enforced.applied)

    advised = {e["item"]: e for e in record.itemAdvice["items"]}
    refused = {
        v.item: v for v in enforced.items if v.decision is not Decision.ALLOW
    }
    assert advised.keys() == refused.keys()
    for name, verdict in refused.items():
        assert advised[name]["decision"] == verdict.decision.value
        assert advised[name]["rule"] == verdict.rule
        assert advised[name]["reasonCode"] == verdict.reason_code


def test_a_killed_item_still_halts_under_advisory() -> None:
    """D-A2 reaches the items too: the operator's cord is not a policy verdict,
    and the records still say which deployment they came from."""
    kill = InMemoryKillStore()
    kill.issue(KillScope.for_session(WORKLIST_RUN.id), issued_by="operator")

    result, world = _worklist(
        "W-1", "W-2", enforcement=EnforcementMode.ADVISORY, kill=kill
    )

    assert result.decision is Decision.HALT
    assert result.applied == ()
    records = world["audit"].records
    assert len(records) == 2, "each halted item is audited on its own"
    for record in records:
        assert record.enforcement is EnforcementMode.ADVISORY
        assert record.decision is Decision.HALT
        assert record.advised is None  # the halt happened; nothing is advice
        assert record.itemAdvice is None


def test_a_call_over_the_ceiling_is_forwarded_not_refused() -> None:
    """A ceiling refusal is not the operator's cord, so advisory does not make
    it: the call goes through unfanned, and the record says the items were never
    judged rather than counting them as allowed."""
    reg = load_registry(
        {"resources": {"WorklistItem": {"actions": {"markManyReviewed": {
            "kind": "record",
            "data": {"itemIds": {"type": "array", "required": True}},
            "items": {"field": "itemIds", "independent": True, "maxItems": 3},
        }}}}}
    )
    audit = InMemoryAuditSink()
    result = enforce(
        _mark("W-1", "W-2", "W-3", "W-4"), WORKLIST_ACTOR, WORKLIST_RUN,
        registry=reg, audit=audit,
        policy=load_policy(
            {"agent": "w", "allow": [{"record": ["markManyReviewed"]}]},
            reg, schema=load_schema(),
        ),
        gates=DefaultGateEngine(reg), outbox=InMemoryOutboxStore(audit=audit),
        connectors=Connectors({"in_memory": InMemoryConnector()}),
        enforcement=EnforcementMode.ADVISORY,
    )

    assert result.decision is Decision.ALLOW
    record = audit.records[-1]
    assert record.parameters["itemIds"] == ["W-1", "W-2", "W-3", "W-4"]
    assert record.enforcement is EnforcementMode.ADVISORY
    assert record.advised is not None
    assert record.advised.decision is Decision.DENY
    assert record.advised.rule == ITEMS_OVER_CEILING
    # It was refused without being looked at, so nothing about it was judged.
    assert record.coverage is Coverage.UNJUDGED
    assert record.itemAdvice is None


class _ProbeFailsForOneItem(InMemoryConnector):
    """Its scope probe cannot reach the store for one payout.

    A dependency failure is a fail-closed reflex, not a judgement of the item —
    which is what makes the record's coverage the honest half of the report.
    """

    def fetch_target(
        self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor
    ) -> Mapping[str, Any] | None:
        if "P-BAD" in (action.data.get("ids") or []):
            raise RuntimeError("target store unreachable")
        return {"owner_id": actor.id}


_PAYOUT_REGISTRY: dict[str, Any] = {
    "scopePredicates": ["assignedToCurrentUser"],
    "resources": {"Payout": {"actions": {"sendMany": {
        "kind": "effect",
        "data": {"ids": {"type": "array", "required": True}},
        "items": {"field": "ids", "independent": True, "maxItems": 10},
    }}}},
}
_PAYOUT_POLICY: dict[str, Any] = {
    "agent": "payouts",
    "allow": [{"effect": ["sendMany"]}],
    "scope": {"Payout": "assignedToCurrentUser"},
}


def _payouts(*ids: str, enforcement: EnforcementMode) -> Any:
    reg = load_registry(_PAYOUT_REGISTRY)
    audit = InMemoryAuditSink()
    policy = load_policy(_PAYOUT_POLICY, reg, schema=load_schema())
    result = enforce(
        RawCall(resource="Payout", action="sendMany", data={"ids": list(ids)}),
        Actor(id="alice"), Session(id="s1"),
        registry=reg, audit=audit, policy=policy, gates=DefaultGateEngine(reg),
        scopes=make_scope_resolver(policy),
        outbox=InMemoryOutboxStore(audit=audit),
        connectors=Connectors({"in_memory": _ProbeFailsForOneItem()}),
        enforcement=enforcement,
    )
    return result, audit


def test_one_unjudged_item_makes_the_whole_call_unjudged() -> None:
    """The record covers the call, so its coverage is the call's — and rounding
    against our own reach is the only safe direction. Which item it was survives
    in the breakdown."""
    enforced, _ = _payouts("P-1", "P-BAD", "P-2", enforcement=EnforcementMode.ENFORCED)
    assert enforced.applied == ("P-1", "P-2")  # fail closed: the probe decided nothing

    result, audit = _payouts(
        "P-1", "P-BAD", "P-2", enforcement=EnforcementMode.ADVISORY
    )

    assert result.applied == ("P-1", "P-BAD", "P-2")
    record = audit.records[-1]
    assert record.coverage is Coverage.UNJUDGED
    assert record.itemAdvice is not None
    entry = next(e for e in record.itemAdvice["items"] if e["item"] == "P-BAD")
    assert entry["rule"] == "scope-unavailable"
    assert entry["coverage"] == "unjudged"
    # ...and the items that WERE judged are not tarred with it.
    assert record.itemAdvice["wouldApply"] == 2


class _BrokenOutbox:
    """Every outbox operation raises: the staging store is down."""

    def __getattr__(self, name: str) -> Any:
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("outbox down")
        return boom


def test_a_real_commit_failure_keeps_its_own_rule_on_the_record() -> None:
    """The commit phase can refuse for real under advisory — a dead outbox, a
    lost scope. That record's deciding rule is the failure's own (spec §11); the
    would-have rule stays in ``advised``, where divergence belongs. Overwriting
    it would misattribute a real refusal to a rule that refused nothing."""
    reg = full_registry()
    policy = load_policy(_DENY_DOC, reg, schema=load_schema())
    audit = InMemoryAuditSink()

    result = enforce(
        RawCall(resource="Email", action="sendEmail", data={"to": "x@acme.example"}),
        Actor(id="alice"), Session(id="s1", correlation_id="corr-1"),
        registry=reg, audit=audit, policy=policy, gates=DefaultGateEngine(reg),
        outbox=_BrokenOutbox(),
        connectors=Connectors({"email": InMemoryConnector()}),
        enforcement=EnforcementMode.ADVISORY,
    )

    assert result.decision is Decision.DENY  # the refusal happened for real
    record = audit.records[-1]
    assert record.decision is Decision.DENY
    assert record.rule == "outbox-unavailable"  # the failure's own rule
    assert record.reasonCode == "outbox-unavailable"  # and they agree
    # The would-have verdict is still there, as divergence, not as the rule.
    assert record.advised is not None
    assert record.advised.rule == "default-deny"
    assert record.enforcement is EnforcementMode.ADVISORY


# --- the second half of a staged effect's story --------------------------
def test_a_dispatched_advisory_effect_stamps_its_settle_record_too() -> None:
    """An effect stages, and the worker settles it later — a second record,
    written outside the pipeline. Without the mode on the row that record reads
    as enforced traffic from a deployment that enforces nothing, and the
    dataset stops being evidence halfway through every effect it contains."""
    from stonefold_store.dispatch import DispatchWorker

    result, audit, outbox = _run(
        _ALLOW_DOC,
        resource="Email",
        action="sendEmail",
        enforcement=EnforcementMode.ADVISORY,
        data={"to": "x@acme.example"},
    )
    assert result.decision is Decision.ALLOW
    assert result.ticket is not None
    # the row itself remembers what it was decided under
    row = outbox.get(result.ticket)
    assert row is not None and row.enforcement is EnforcementMode.ADVISORY

    reg = full_registry()
    worker = DispatchWorker(
        outbox,
        Connectors({"email": InMemoryConnector(), "in_memory": InMemoryConnector(),
                    "sql": InMemoryConnector()}),
        registry=reg,
    )
    assert worker.drain() == 1

    settle = audit.records[-1]
    assert settle.rule == "dispatch"  # the worker's record, not the pipeline's
    assert settle.enforcement is EnforcementMode.ADVISORY
    # ...and every record of this run agrees about the deployment it came from.
    assert {r.enforcement for r in audit.records} == {EnforcementMode.ADVISORY}


def test_an_enforcing_deployment_settles_enforced() -> None:
    """The label follows the action, not the wiring: the same worker and the
    same sink settle an enforcing row as enforced."""
    from stonefold_store.dispatch import DispatchWorker

    result, audit, outbox = _run(
        _ALLOW_DOC,
        resource="Email",
        action="sendEmail",
        enforcement=EnforcementMode.ENFORCED,
        data={"to": "x@acme.example"},
    )
    assert result.ticket is not None
    worker = DispatchWorker(
        outbox,
        Connectors({"email": InMemoryConnector(), "in_memory": InMemoryConnector(),
                    "sql": InMemoryConnector()}),
        registry=full_registry(),
    )
    assert worker.drain() == 1
    assert {r.enforcement for r in audit.records} == {EnforcementMode.ENFORCED}


def test_a_cancelled_advisory_row_is_stamped_as_well() -> None:
    """The sweeps write records too — a stale decision, an expired hold, a kill
    inside the claim. Each is part of its action's story, so each carries the
    mode that action was decided under rather than the sweeper's."""
    from stonefold_core.outbox import cancellation_record

    reg = full_registry()
    resolved = reg.resolve(
        RawCall(resource="Email", action="sendEmail", data={"to": "x@acme.example"})
    )
    audit = InMemoryAuditSink()
    outbox = InMemoryOutboxStore(audit=audit)
    row = outbox.stage(
        resolved=resolved, actor=Actor(id="alice"), session_id="s1",
        agent="support", state=PendingState.PENDING,
        enforcement=EnforcementMode.ADVISORY,
    )

    record = cancellation_record(row, "stale-decision")

    assert record.enforcement is EnforcementMode.ADVISORY
    assert record.outcome == "cancelled"
    assert record.rule == "stale-decision"
